// SPDX-License-Identifier: MIT
// Kairix RealLines RP2040 acquisition appliance, matched with app/pico_protocol.py.
// Encoders GP0/1, GP2/3, GP4/5. Buttons GP10/11/12 active-low.
// UART1 GP8 TX -> Pi RX GPIO15; GP9 RX reserved; 1,000,000 baud, 8N1.
// Never send human-readable logs on the binary UART.

#include <stdbool.h>
#include <stdint.h>
#include <string.h>

#include "pico/stdlib.h"
#include "pico/rand.h"
#include "hardware/pio.h"
#include "hardware/uart.h"
#include "hardware/watchdog.h"
#include "quadrature_encoder.pio.h"

#define UART uart1
#define UART_BAUD 1000000
#define UART_TX_PIN 8
#define UART_RX_PIN 9
#define BUTTON_COUNT 3
#define FRAME_BYTES 58
#define FRAME_INTERVAL_US 2000
#define DEBOUNCE_US 20000

typedef struct {
    bool last_raw;
    bool stable;
    uint32_t last_change_us;
    uint16_t presses;
} button_t;

static void put16(uint8_t *dst, uint16_t x) {
    dst[0] = (uint8_t)x;
    dst[1] = (uint8_t)(x >> 8);
}
static void put32(uint8_t *dst, uint32_t x) {
    for (unsigned i = 0; i < 4; i++) dst[i] = (uint8_t)(x >> (8 * i));
}
static void put64(uint8_t *dst, uint64_t x) {
    for (unsigned i = 0; i < 8; i++) dst[i] = (uint8_t)(x >> (8 * i));
}
static uint16_t crc16(const uint8_t *bytes, unsigned size) {
    uint16_t crc = 0xFFFF;
    for (unsigned i = 0; i < size; i++) {
        crc ^= (uint16_t)bytes[i] << 8;
        for (unsigned j = 0; j < 8; j++)
            crc = (crc & 0x8000) ? (uint16_t)((crc << 1) ^ 0x1021) : (uint16_t)(crc << 1);
    }
    return crc;
}
static void poll_buttons(button_t buttons[BUTTON_COUNT], uint32_t now) {
    for (unsigned i = 0; i < BUTTON_COUNT; i++) {
        bool down = !gpio_get(10 + i);
        if (down != buttons[i].last_raw) {
            buttons[i].last_raw = down;
            buttons[i].last_change_us = now;
        }
        if ((uint32_t)(now - buttons[i].last_change_us) >= DEBOUNCE_US && down != buttons[i].stable) {
            buttons[i].stable = down;
            if (down) buttons[i].presses++;
        }
    }
}

int main(void) {
    stdio_init_all();               // USB stdio only; compatible with picotool -f update.
    uart_init(UART, UART_BAUD);
    gpio_set_function(UART_TX_PIN, GPIO_FUNC_UART);
    gpio_set_function(UART_RX_PIN, GPIO_FUNC_UART);
    uart_set_format(UART, 8, 1, UART_PARITY_NONE);
    uart_set_fifo_enabled(UART, true);

    PIO pio = pio0;
    pio_add_program(pio, &quadrature_encoder_program); // Must be at address 0.
    for (unsigned i = 0; i < 3; i++) {
        quadrature_encoder_program_init(pio, i, i * 2, 0);
    }
    button_t buttons[BUTTON_COUNT] = {0};
    for (unsigned i = 0; i < BUTTON_COUNT; i++) {
        gpio_init(10 + i);
        gpio_set_dir(10 + i, GPIO_IN);
        gpio_pull_up(10 + i);
        buttons[i].last_change_us = time_us_32();
    }
    uint32_t session = get_rand_32();
    if (!session) session = 1;
    uint32_t sequence = 0;
    int32_t previous[3] = {0};
    int64_t cumulative[3] = {0};
    // The PIO counts independently; snapshotting doesn't alter the count.
    for (unsigned i = 0; i < 3; i++) previous[i] = quadrature_encoder_get_count(pio, i);
    watchdog_enable(2000, true);
    uint64_t deadline = time_us_64();
    uint8_t frame[FRAME_BYTES];
    while (true) {
        watchdog_update();
        uint32_t now = time_us_32();
        poll_buttons(buttons, now);
        for (unsigned i = 0; i < 3; i++) {
            int32_t current = quadrature_encoder_get_count(pio, i);
            int32_t delta = (int32_t)((uint32_t)current - (uint32_t)previous[i]);
            previous[i] = current;
            cumulative[i] += delta;
        }
        memset(frame, 0, sizeof(frame));
        memcpy(frame, "KRP1", 4);
        put32(frame + 4, sequence++);
        put32(frame + 8, session);
        put64(frame + 12, time_us_64());
        put64(frame + 20, (uint64_t)cumulative[0]);
        put64(frame + 28, (uint64_t)cumulative[1]);
        put64(frame + 36, (uint64_t)cumulative[2]);
        uint8_t mask = 0;
        for (unsigned i = 0; i < BUTTON_COUNT; i++) {
            if (buttons[i].stable) mask |= (uint8_t)(1u << i);
            put16(frame + 45 + 2 * i, buttons[i].presses);
        }
        frame[44] = mask;
        frame[51] = 0; // Firmware fault flags (reserved for future encoder diagnostics).
        put32(frame + 52, to_ms_since_boot(get_absolute_time()));
        put16(frame + 56, crc16(frame, 56));
        uart_write_blocking(UART, frame, sizeof(frame));
        deadline += FRAME_INTERVAL_US;
        uint64_t latest = time_us_64();
        if (deadline < latest) deadline = latest; // Skip deadlines, never skip counts.
        while (time_us_64() < deadline) {
            watchdog_update();
            tight_loop_contents();
        }
    }
}
