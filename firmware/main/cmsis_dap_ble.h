#pragma once

#include "DAP_gpio_config.h"
#include "esp_err.h"
#include <stdbool.h>

esp_err_t cmsis_dap_ble_start(const struct cmsis_dap_gpio_config *gpio,
                              const char *mac);
bool cmsis_dap_ble_is_connected(void);
void cmsis_dap_ble_ensure_advertising(void);
