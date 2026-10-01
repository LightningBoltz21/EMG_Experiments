#include <zephyr/kernel.h>
#include <zephyr/device.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/logging/log.h>
#include <string.h>
#include <zephyr/sys/printk.h>
#include "ads1298.h"
#include <zephyr/drivers/spi.h>
#include <zephyr/drivers/pwm.h>
#include <zephyr/bluetooth/bluetooth.h>
#include <zephyr/bluetooth/conn.h>
#include <bluetooth/services/nus.h>
#include <zephyr/bluetooth/uuid.h>
#include <zephyr/bluetooth/gatt.h>
#include <zephyr/bluetooth/hci.h>

LOG_MODULE_REGISTER(emg_software, LOG_LEVEL_INF);

// --- Bluetooth Configuration ---
static struct bt_conn *current_conn;
static const struct bt_data ad[] = {
    BT_DATA_BYTES(BT_DATA_FLAGS, (BT_LE_AD_GENERAL | BT_LE_AD_NO_BREDR)),
    BT_DATA(BT_DATA_NAME_COMPLETE, "EMG Device", 10),
};

// NUS callback for received data
static void nus_cb_received(struct bt_conn *conn, const uint8_t *const data,
                           uint16_t len)
{
    LOG_INF("Received data, len %d", len);
}

// NUS callback for sent data
static void nus_cb_sent(struct bt_conn *conn)
{
    LOG_INF("Data sent");
}

static struct bt_nus_cb nus_cb = {
    .received = nus_cb_received,
    .sent = nus_cb_sent,
};

// Connection callback
static void connected(struct bt_conn *conn, uint8_t err)
{
    if (err) {
        LOG_ERR("Connection failed (err %u)", err);
        return;
    }

    current_conn = bt_conn_ref(conn);
    LOG_INF("Connected");
}

// Disconnection callback
static void disconnected(struct bt_conn *conn, uint8_t reason)
{
    LOG_INF("Disconnected (reason %u)", reason);

    if (current_conn) {
        bt_conn_unref(current_conn);
        current_conn = NULL;
    }
}

static struct bt_conn_cb conn_callbacks = {
    .connected = connected,
    .disconnected = disconnected,
};


// Define explicit parameters without the deprecated flag
static struct bt_le_adv_param adv_param = BT_LE_ADV_PARAM_INIT(
    BT_LE_ADV_OPT_CONNECTABLE, // Removed BT_LE_ADV_OPT_USE_NAME to avoid err -22
    BT_GAP_ADV_FAST_INT_MIN_2,                         // 100ms
    BT_GAP_ADV_FAST_INT_MAX_2,                         // 150ms
    NULL);

// Initialize Bluetooth
static int ble_init(void)
{
    int err;

    // Enable Bluetooth
    err = bt_enable(NULL);
    if (err) {
        LOG_ERR("Bluetooth init failed (err %d)", err);
        return err;
    }

    LOG_INF("Bluetooth initialized");

    // Initialize NUS service
    err = bt_nus_init(&nus_cb);
    if (err) {
        LOG_ERR("Failed to init NUS (err %d)", err);
        return err;
    }

    // Register connection callbacks
    bt_conn_cb_register(&conn_callbacks);

    // 4. Start advertising
    err = bt_le_adv_start(&adv_param, ad, ARRAY_SIZE(ad), NULL, 0);
    if (err) {
        LOG_ERR("Advertising failed to start (err %d)", err);
        return 0;
    }

    LOG_INF("Advertising successfully started");
    return 0;
}

// --- SPI Configuration ---
#define SPIOP (SPI_WORD_SET(8) | SPI_TRANSFER_MSB | SPI_MODE_CPHA)
struct spi_dt_spec spispec = SPI_DT_SPEC_GET(DT_NODELABEL(gendev), SPIOP, 0);

// --- PWM Configuration (ADS1298 master clock, 2 MHz on P1.04) ---
static const struct device *pwm_dev = DEVICE_DT_GET(DT_NODELABEL(pwm0));

static int ads_clk_start(void)
{
    if (!device_is_ready(pwm_dev)) {
        LOG_ERR("PWM device not ready");
        return -1;
    }
    /* 2 MHz square wave, 50% duty. period = 500 ns, pulse = 250 ns. */
    int err = pwm_set(pwm_dev, 0, PWM_NSEC(500), PWM_NSEC(250), 0);
    if (err) {
        LOG_ERR("pwm_set failed: %d", err);
        return err;
    }
    LOG_INF("ADS1298 master clock (2 MHz) started on P1.04");
    return 0;
}

// --- GPIO Configuration ---
#define ZEPHYR_USER_NODE DT_PATH(zephyr_user)
const struct gpio_dt_spec ads_reset = GPIO_DT_SPEC_GET(ZEPHYR_USER_NODE, ads_reset_gpios);
const struct gpio_dt_spec ads_drdy  = GPIO_DT_SPEC_GET(ZEPHYR_USER_NODE, ads_drdy_gpios);

struct gpio_callback drdy_cb_data;
// Define a work item to handle the SPI read outside the ISR
struct k_work drdy_work;

// --- Helper: Write Single Register ---
int ads_write_reg(uint8_t reg_addr, uint8_t value)
{
    int err;
    uint8_t tx_data[3];
    
    // Command: WREG (0x40) + Register Address
    tx_data[0] = WREG | reg_addr; 
    // Operand: Number of registers to write - 1 (0x00 for 1 register)
    tx_data[1] = 0x00;            
    // Payload: The value to write
    tx_data[2] = value;           

    struct spi_buf tx_buf = {.buf = tx_data, .len = 3};
    struct spi_buf_set tx_set = {.buffers = &tx_buf, .count = 1};

    // Note: We don't need to read back MISO during a WREG, so rx_set is NULL
    err = spi_write_dt(&spispec, &tx_set);
    if (err < 0) {
        LOG_ERR("Failed to write Reg 0x%02x: %d", reg_addr, err);
    } else {
        LOG_INF("Wrote 0x%02x to Reg 0x%02x", value, reg_addr);
    }
    return err;
}

// --- Helper: Send Command ---
int ads_send_cmd(uint8_t cmd)
{
    struct spi_buf tx_buf = {.buf = &cmd, .len = 1};
    struct spi_buf_set tx_set = {.buffers = &tx_buf, .count = 1};
    return spi_write_dt(&spispec, &tx_set);
}

// --- Helper: Send EMG data over BLE as binary ---
// Sends the raw microvolt value as a signed 16-bit little-endian int.
// Range: +/-32.768 mV, resolution: 1 uV. Clamped if ch1_uv exceeds int16 range.
static void send_emg_data_ble_binary(int32_t ch1_uv)
{
    if (!current_conn) {
        return;
    }

    if (ch1_uv > INT16_MAX) ch1_uv = INT16_MAX;
    if (ch1_uv < INT16_MIN) ch1_uv = INT16_MIN;
    int16_t value = (int16_t)ch1_uv;

    uint8_t buffer[2] = { (uint8_t)(value & 0xFF), (uint8_t)((value >> 8) & 0xFF) };
    bt_nus_send(current_conn, buffer, sizeof(buffer));
}

// --- Helper: Read Data Packet ---
// This now matches the signature required by k_work_handler_t
void read_ads1298_data(struct k_work *item)
{
    int err;
    // 27 bytes = 3 status + (8 channels * 3 bytes)
    uint8_t tx_dummy[27] = {0}; 
    uint8_t rx_data[27]  = {0};

    struct spi_buf tx_buf = {.buf = tx_dummy, .len = sizeof(tx_dummy)};
    struct spi_buf rx_buf = {.buf = rx_data,  .len = sizeof(rx_data)};
    struct spi_buf_set tx_set = {.buffers = &tx_buf, .count = 1};
    struct spi_buf_set rx_set = {.buffers = &rx_buf, .count = 1};

    err = spi_transceive_dt(&spispec, &tx_set, &rx_set);
    if (err < 0) {
        LOG_ERR("Data Read Fail: %d", err);
        return;
    }

    // Extract CH1 (Bytes 3, 4, 5)
    int32_t ch1_raw = (rx_data[3] << 16) | (rx_data[4] << 8) | rx_data[5];
    
    // Sign extension for 24-bit
    if (ch1_raw & 0x800000) {
        ch1_raw |= 0xFF000000; 
    }

    // --- Convert Raw to Millivolts (mV) ---
    // V_REF = 2.4V (set in CONFIG3)
    // Gain = 6 (set in CH1SET)
    // Max positive 24-bit value = 2^23 - 1 = 8,388,607
    // Voltage (uV) = (Raw * V_REF) / ((2^23 - 1) * Gain)
    // Voltage (uV) = (Raw * 2400000) / (8388607 * 6)
    // Voltage (uV) = (Raw * 400000) / 8388607
    // We use 64-bit integer math to prevent overflow and avoid float logging issues in Zephyr.
    int32_t ch1_uv = (int32_t)(((int64_t)ch1_raw * 400000) / 8388607);

    // Send every sample over BLE so the iOS pipeline (bandpass 20-150 Hz)
    // has a meaningful Nyquist. 500 Hz * 2 B = 1 kB/s, well within NUS.
    send_emg_data_ble_binary(ch1_uv);

    // Removed the log_counter decimation!
    int32_t mv_int = ch1_uv / 1000;
    int32_t mv_frac = ch1_uv % 1000;
    if (mv_frac < 0) mv_frac = -mv_frac;

    char buffer[32];
    if (ch1_uv < 0 && mv_int == 0) {
        snprintf(buffer, sizeof(buffer), ">CH1:-0.%03d\n", mv_frac);
    } else {
        snprintf(buffer, sizeof(buffer), ">CH1:%d.%03d\n", mv_int, mv_frac);
    }
    printk("%s", buffer);
}

// --- Interrupt Handler ---
void drdy_handler(const struct device *dev, struct gpio_callback *cb, uint32_t pins)
{
    // CRITICAL FIX: Do NOT call SPI functions here.
    // Instead, submit the work to the system work queue.
    // The kernel will run read_ads1298_data() as soon as the ISR finishes.
    k_work_submit(&drdy_work);
}

// --- Initialization ---
int ads_init(void)
{
    // 1. Initialize the Work Queue Item
    k_work_init(&drdy_work, read_ads1298_data);

    // 2. GPIO Init
    if (!gpio_is_ready_dt(&ads_reset) || !gpio_is_ready_dt(&ads_drdy)) return -1;
    gpio_pin_configure_dt(&ads_reset, GPIO_OUTPUT_INACTIVE);
    gpio_pin_configure_dt(&ads_drdy, GPIO_INPUT);
    gpio_pin_interrupt_configure_dt(&ads_drdy, GPIO_INT_EDGE_TO_ACTIVE);
    gpio_init_callback(&drdy_cb_data, drdy_handler, BIT(ads_drdy.pin));
    gpio_add_callback(ads_drdy.port, &drdy_cb_data);

    // 3. Hardware Reset
    LOG_INF("Resetting...");
    gpio_pin_set_dt(&ads_reset, 1);
    k_sleep(K_MSEC(2)); 
    gpio_pin_set_dt(&ads_reset, 0);
    k_sleep(K_MSEC(150)); 

    // 4. Send WAKEUP and SDATAC (REQUIRED before writing registers)
    ads_send_cmd(WAKEUP);
    k_sleep(K_MSEC(2));
    ads_send_cmd(SDATAC);
    k_sleep(K_MSEC(10));

    return 0;
}

int main(void)
{
    // Initialize Bluetooth first
    if (ble_init() < 0) {
        LOG_ERR("Bluetooth init failed");
        return 0;
    }

    if (!spi_is_ready_dt(&spispec)) {
        LOG_ERR("SPI not ready");
        return 0;
    }

    /* Start the external clock for the ADS1298 before resetting it. */
    if (ads_clk_start() < 0) {
        LOG_ERR("ADS clock start failed");
        return 0;
    }
    k_sleep(K_MSEC(100)); // Wait for tPOR (at least 32ms) before resetting

    if (ads_init() < 0) {
        LOG_ERR("GPIO init failed");
        return 0;
    }

    // --- Sensor Configuration ---

    // --- STEP 0: Set High-Resolution Mode & Data Rate ---
    // Register: CONFIG1 (0x01)
    // Value: 0x86 -> High-Resolution 500SPS
    // HR=1 (High-res), CLK_EN=0, DR[2:0]=110 (500SPS)
    ads_write_reg(CONFIG1, HIGH_RES_500_SPS);
    k_sleep(K_MSEC(1));

    // --- STEP 1: Configure Reference and RLD ---
    // Register: CONFIG3 (0x03)
    // Value: 0xD8 -> 0b11011000
    // PD_REFBUF = 1 (internal reference enabled, VREF = 2.4V)
    // VREF_4V = 0 (VREF = 2.4V)
    // RLD_MEAS = 1 (RLD measurement routed to ADC)
    // RLDREF_INT = 1 (RLD reference is internal (AVDD-AVSS)/2)
    // PD_RLD = 0 (RLD buffer is enabled)
    ads_write_reg(CONFIG3, CONFIG3_const | PD_REFBUF | RLD_MEAS | RLDREF_INT);
    k_sleep(K_MSEC(100)); // Allow reference to settle

    // --- STEP 2: Disable Internal Test Signal Generator ---
    // Register: CONFIG2 (0x02)
    // Value: 0x00 -> Test signals disabled, default state
    // ads_write_reg(CONFIG2, 0x00);
    // k_sleep(K_MSEC(1));

    // --- STEP 3: Configure Channel 1 ---
    // Register: CH1SET (0x05)
    // We want Gain=6 and Mux=Electrode Input
    // GAIN_6X (0x00) | ELECTRODE_INPUT (0x00) = 0x00
    ads_write_reg(CH1SET, GAIN_6X | ELECTRODE_INPUT);
    k_sleep(K_MSEC(1));

    // Optional: Power down other channels (CH2-CH8) to reduce noise/power
    // Writing 0x81 (PD=1, Input=Short) to CH2SET
    // ads_write_reg(CH2SET, 0x81); 

    // --- SANITY: Read ID register (expect 0x92 for ADS1298) ---
    {
        uint8_t tx[3] = { RREG | 0x00, 0x00, 0x00 };
        uint8_t rx[3] = { 0 };
        struct spi_buf txb = { .buf = tx, .len = 3 };
        struct spi_buf rxb = { .buf = rx, .len = 3 };
        struct spi_buf_set txs = { .buffers = &txb, .count = 1 };
        struct spi_buf_set rxs = { .buffers = &rxb, .count = 1 };
        int e = spi_transceive_dt(&spispec, &txs, &rxs);
        LOG_INF("ID reg read err=%d, bytes: 0x%02x 0x%02x 0x%02x (expect last byte 0x92)",
                e, rx[0], rx[1], rx[2]);
        k_sleep(K_MSEC(5));
    }

    // --- STEP 4: Start Conversion ---
    LOG_INF("Starting Data Stream...");
    ads_send_cmd(START);  // Start conversions
    k_sleep(K_MSEC(1));
    ads_send_cmd(RDATAC); // Put in Read Data Continuous mode

    // From now on, whenever DRDY goes low, the drdy_handler will print the data
    while (1) {
        k_sleep(K_FOREVER);
    }
    return 0;
}