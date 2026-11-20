#pragma once

#include <stdbool.h>

bool probe_link_use_ble(void);
int probe_link_command(int argc, char **argv);
void probe_link_start_discovery(const char *mac);
