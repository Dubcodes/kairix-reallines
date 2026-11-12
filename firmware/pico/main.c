// Kairix RealLines RP2040 controller v1. UART encoder snapshots; USB for updating.
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
#include "pico/stdlib.h"
#include "pico/rand.h"
#include "hardware/pio.h"
#include "hardware/uart.h"
#include "hardware/watchdog.h"
#include "quadrature_encoder.pio.h"

#define PROTOCOL_VERSION 1u
#define REPORT_INTERVAL_US 2000u  // 500 Hz, not the encoder counting rate
#define UART_BAUD 1000000u
#define UART_TX_PIN 8u
#define UART_RX_PIN 9u
#define AXES 4u
#define ACTIVE_AXES 3u
#define BUTTONS 3u
#define BUTTON_DEBOUNCE_MS 20u
#define PACKET_SIZE 63u
#define LED_PIN 25u

static const uint8_t axis_pins[AXES] = {0, 2, 4, 6};
static const uint8_t button_pins[BUTTONS] = {10, 11, 12};

// Wire protocol little-endian. All frames contain cumulative counts.
typedef struct __attribute__((packed)) {
    uint8_t magic[2];             // A5 5A
    uint8_t version;              // 1
    uint8_t frame_type;           // 1 = tracking
    uint32_t sequence;
    uint64_t timestamp_us;        // Pico clock (NOT host clock)
    uint32_t session_id;          // changes on every firmware boot
    int64_t counts[AXES];         // raw x4 counts; focus reserved
    uint8_t buttons_pressed;      // bit n = currently held
    uint16_t button_presses[BUTTONS]; // monotonic modulo-65536 press events
    uint16_t flags;               // bit 0: watchdog reset, bit 1: focus inactive
    uint16_t crc16;               // CRC-16/CCITT-FALSE excluding this field
} tracking_packet_t;
_Static_assert(sizeof(tracking_packet_t) == PACKET_SIZE, "Unexpected packet padding");

static uint16_t crc16_ccitt(const uint8_t *data, size_t len) {
    uint16_t crc = 0xffff;
    for (size_t i = 0; i < len; ++i) {
        crc ^= (uint16_t)data[i] << 8;
        for (int b = 0; b < 8; ++b)
            crc = (crc & 0x8000) ? (uint16_t)((crc << 1) ^ 0x1021) : (uint16_t)(crc << 1);
    }
    return crc;
}

typedef struct {
    bool raw_down;
    bool stable_down;
    uint32_t raw_changed_ms;
    uint16_t presses;
} button_t;
static button_t buttons[BUTTONS];

static uint8_t poll_buttons(uint32_t now_ms) {
    uint8_t mask = 0;
    for (unsigned i = 0; i < BUTTONS; ++i) {
        bool down = !gpio_get(button_pins[i]);
        button_t *b = &buttons[i];
        if (down != b->raw_down) {
            b->raw_down = down;
            b->raw_changed_ms = now_ms;
        }
        if (down != b->stable_down && (uint32_t)(now_ms - b->raw_changed_ms) >= BUTTON_DEBOUNCE_MS) {
            b->stable_down = down;
            if (down) ++b->presses;
        }
        if (b->stable_down) mask |= (uint8_t)(1u << i);
    }
    return mask;
}

static void encoder_init(PIO pio, uint sm, uint pin) {
    pio_sm_set_enabled(pio, sm, false);
    pio_gpio_init(pio, pin);
    pio_gpio_init(pio, pin + 1);
    pio_sm_set_consecutive_pindirs(pio, sm, pin, 2, false);
    gpio_pull_up(pin);       // external pull-ups are still recommended
    gpio_pull_up(pin + 1);
    pio_sm_config c = quadrature_encoder_program_get_default_config(0);
    sm_config_set_in_pins(&c, pin);
    sm_config_set_jmp_pin(&c, pin);
    sm_config_set_in_shift(&c, false, false, 32);
    sm_config_set_fifo_join(&c, PIO_FIFO_JOIN_NONE);
    sm_config_set_clkdiv(&c, 1.0f);
    pio_sm_init(pio, sm, 0, &c);
    pio_sm_set_enabled(pio, sm, true);
}

static uint32_t encoder_count(PIO pio, uint sm) {
    // Drain old FIFO samples and fetch a fresh sample; the PIO never blocks
    // while publishing and continues accumulating every quadrature transition.
    int samples = pio_sm_get_rx_fifo_level(pio, sm) + 1;
    uint32_t value = 0;
    while (samples--) value = pio_sm_get_blocking(pio, sm);
    return value;
}

int main(void) {
    const bool watchdog_reset = watchdog_caused_reboot();
    stdio_init_all(); // USB vendor reset interface for picotool; never UART stdio
    uart_init(uart1, UART_BAUD);
    uart_set_format(uart1, 8, 1, UART_PARITY_NONE);
    gpio_set_function(UART_TX_PIN, GPIO_FUNC_UART);
    gpio_set_function(UART_RX_PIN, GPIO_FUNC_UART);
    gpio_init(LED_PIN); gpio_set_dir(LED_PIN, GPIO_OUT);

    for (unsigned i = 0; i < BUTTONS; ++i) {
        gpio_init(button_pins[i]);
        gpio_set_dir(button_pins[i], GPIO_IN);
        gpio_pull_up(button_pins[i]);
        buttons[i].raw_down = !gpio_get(button_pins[i]);
        buttons[i].stable_down = buttons[i].raw_down;
        buttons[i].raw_changed_ms = to_ms_since_boot(get_absolute_time());
    }
    const PIO pio = pio0;
    pio_add_program(pio, &quadrature_encoder_program); // program .origin 0
    for (unsigned i = 0; i < ACTIVE_AXES; ++i) encoder_init(pio, i, axis_pins[i]);

    uint32_t previous[ACTIVE_AXES] = {0};
    int64_t totals[AXES] = {0};
    // The initial counter phase may not be zero; use first reading as relative origin.
    for (unsigned i = 0; i < ACTIVE_AXES; ++i) previous[i] = encoder_count(pio, i);

    tracking_packet_t packet = {0};
    packet.magic[0] = 0xa5; packet.magic[1] = 0x5a;
    packet.version = PROTOCOL_VERSION;
    packet.frame_type = 1;
    packet.session_id = get_rand_32();
    packet.flags = (watchdog_reset ? 1u : 0u) | 2u | (1u << 8); // firmware v1.0
    uint64_t next_report = time_us_64();
    uint32_t last_led_ms = 0;
    uint8_t pressed = 0;
    watchdog_enable(2000, true);

    while (true) {
        uint64_t now = time_us_64();
        pressed = poll_buttons((uint32_t)(now / 1000));
        if (now >= next_report) {
            // Never force the encoder hardware to the reporting frequency.
            for (unsigned i = 0; i < ACTIVE_AXES; ++i) {
                uint32_t raw = encoder_count(pio, i);
                int32_t delta = (int32_t)(raw - previous[i]);
                totals[i] += (int64_t)delta;
                previous[i] = raw;
            }
            packet.sequence++;
            packet.timestamp_us = time_us_64();
            for (unsigned i = 0; i < AXES; ++i) packet.counts[i] = totals[i];
            packet.buttons_pressed = pressed;
            for (unsigned i = 0; i < BUTTONS; ++i) packet.button_presses[i] = buttons[i].presses;
            packet.crc16 = crc16_ccitt((const uint8_t *)&packet, PACKET_SIZE - 2);
            uart_write_blocking(uart1, (const uint8_t *)&packet, PACKET_SIZE);
            // Skip elapsed reporting slots, never queue old position data.
            next_report += REPORT_INTERVAL_US;
            if (next_report <= time_us_64()) next_report = time_us_64() + REPORT_INTERVAL_US;
        }
        uint32_t ms = (uint32_t)(now / 1000);
        if ((uint32_t)(ms - last_led_ms) >= 500) {
            gpio_put(LED_PIN, !gpio_get(LED_PIN)); last_led_ms = ms;
        }
        watchdog_update();
        sleep_us(250);
    }
}
