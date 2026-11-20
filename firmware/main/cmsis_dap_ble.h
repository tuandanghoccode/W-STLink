#pragma once

#include "esp_err.h"
#include "DAP_gpio_config.h"

esp_err_t cmsis_dap_ble_start(const struct cmsis_dap_gpio_config *gpio, const char *mac);
