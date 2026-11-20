#pragma once

#include "esp_err.h"
#include <stdbool.h>

bool probe_link_use_ble(void);
esp_err_t probe_link_set_ble(bool use_ble);
int probe_link_command(int argc, char **argv);
void probe_link_start_discovery(const char *mac);
