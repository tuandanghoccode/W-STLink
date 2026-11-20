/* Modified for stm32_wdebug_tool to report unconfigured Station mode and
 * support multi-AP storage. */

#ifndef WIFI_H
#define WIFI_H

#ifdef __cplusplus
extern "C" {
#endif

#include "esp_err.h"
#include "esp_wifi_types_generic.h"
#include <stdbool.h>
#include <stdint.h>

#define WIFI_MAX_SAVED_APS 5

typedef struct {
  char ssid[33];
  char password[65];
  uint8_t auth_mode;
} wifi_saved_ap_t;

typedef enum {
  WIFI_OPERATING_MODE_STA = 0,
  WIFI_OPERATING_MODE_AP = 1,
} wifi_operating_mode_t;

// Initializes one WiFi interface, selected by NVS override or menuconfig.
// Station mode cycles through up to 5 saved APs (from latest to 5th).
// On successful connection, the connected AP is promoted to index 0 (MRU).
// Returns ESP_OK on success, ESP_ERR_NOT_FOUND when Station is unconfigured,
// or ESP_FAIL when the connection fails.
esp_err_t wifi_init(void);

// Adds or promotes an AP to index 0 in NVS (storing up to WIFI_MAX_SAVED_APS).
esp_err_t wifi_add_or_promote_ap(const char *ssid, const char *password,
                                 wifi_auth_mode_t auth_mode);

// Retrieves the list of currently saved APs (up to max_count). Returns count.
int wifi_get_saved_aps(wifi_saved_ap_t *list, int max_count);

// Tells wifi.c that a network-dependent TCP service (CMSIS-DAP-TCP) has
// started listening. From this point on, losing the AP connection reboots
// the device instead of just retrying -- the listening/connected sockets
// can't be recovered without reinitializing the whole network stack.
void wifi_mark_service_started(void);

// Reports current STA connectivity or SoftAP SSID. *rssi is only valid in
// Station mode when *connected is true.
void wifi_get_status(bool *connected, const char **ssid, int *rssi);

// Write new WiFi credentials to flash (promotes to slot 0).
esp_err_t wifi_save_config(const char *ssid, const char *password,
                           wifi_auth_mode_t auth_mode);

// Erase credentials from flash. Subsequent boots fall back to CONFIG defaults.
esp_err_t wifi_clear_config(void);

// SoftAP settings are independent of Station credentials. Takes effect after
// reboot. Omitted command-line channel uses the menuconfig default.
esp_err_t wifi_save_ap_config(const char *ssid, const char *password,
                              uint8_t channel);
esp_err_t wifi_clear_ap_config(void);

// Exactly one WiFi mode is active. Changes persist in NVS and take effect
// after reboot; clearing the override restores the menuconfig default.
wifi_operating_mode_t wifi_get_operating_mode(void);
wifi_operating_mode_t wifi_get_default_operating_mode(void);
// Mode that will be active after the next reboot (NVS override or default).
wifi_operating_mode_t wifi_get_selected_operating_mode(void);
esp_err_t wifi_save_operating_mode(wifi_operating_mode_t mode);
esp_err_t wifi_clear_operating_mode(void);

// Reports the active SoftAP channel (valid when AP mode is active).
uint8_t wifi_get_ap_channel(void);
// True if NVS overrides or menuconfig provide a valid WPA2 SoftAP setup.
bool wifi_ap_is_configured(void);
// True if the menuconfig SoftAP defaults alone form a valid WPA2 setup.
bool wifi_ap_defaults_valid(void);

#ifdef __cplusplus
}
#endif

#endif // WIFI_H
