#include "probe_link.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "lwip/sockets.h"
#include "nvs.h"
#include <errno.h>
#include <stdio.h>
#include <string.h>

static const char nvs_namespace[] = "dap_link";
static const char key[] = "ble";
static const char discovery_query[] = "STM32-WDEBUG/1 DISCOVER";
static char discovery_reply[sizeof("STM32-WDEBUG/1 000000000000")];

bool probe_link_use_ble(void) {
  nvs_handle_t settings;
  uint8_t use_ble = 0;
  if (nvs_open(nvs_namespace, NVS_READONLY, &settings) == ESP_OK) {
    nvs_get_u8(settings, key, &use_ble);
    nvs_close(settings);
  }
  return use_ble == 1;
}

esp_err_t probe_link_set_ble(bool use_ble) {
  nvs_handle_t settings;
  esp_err_t err = nvs_open(nvs_namespace, NVS_READWRITE, &settings);
  if (err == ESP_OK) {
    err = nvs_set_u8(settings, key, use_ble ? 1 : 0);
    if (err == ESP_OK)
      err = nvs_commit(settings);
    nvs_close(settings);
  }
  return err;
}

int probe_link_command(int argc, char **argv) {
  if (argc == 1) {
    printf("Link mode: %s\n", probe_link_use_ble() ? "ble" : "wifi");
    return 0;
  }
  if (argc != 2 ||
      (strcmp(argv[1], "wifi") != 0 && strcmp(argv[1], "ble") != 0)) {
    printf("Usage: link [wifi|ble]\n");
    return 1;
  }

  esp_err_t err = probe_link_set_ble(strcmp(argv[1], "ble") == 0);
  if (err != ESP_OK) {
    printf("Could not save link mode: %s\n", esp_err_to_name(err));
    return 1;
  }
  printf("Link mode saved: %s. Run reboot to apply.\n", argv[1]);
  return 0;
}

static void discovery_task(void *unused) {
  int sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
  if (sock < 0) {
    ESP_LOGW("probe_link", "Could not open UDP discovery socket");
    vTaskDelete(NULL);
    return;
  }
  const struct sockaddr_in address = {
      .sin_family = AF_INET,
      .sin_port = htons(4443),
      .sin_addr.s_addr = htonl(INADDR_ANY),
  };
  if (bind(sock, (const struct sockaddr *)&address, sizeof(address)) == 0) {
    for (;;) {
      char received[sizeof(discovery_query)];
      struct sockaddr_in sender;
      socklen_t sender_len = sizeof(sender);
      ssize_t length = recvfrom(sock, received, sizeof(received), 0,
                                (struct sockaddr *)&sender, &sender_len);
      if (length < 0) {
        if (errno == EINTR)
          continue;
        break;
      }
      if (length == sizeof(discovery_query) - 1 &&
          memcmp(received, discovery_query, length) == 0) {
        sendto(sock, discovery_reply, sizeof(discovery_reply) - 1, 0,
               (const struct sockaddr *)&sender, sender_len);
      }
    }
  }
  ESP_LOGW("probe_link", "UDP discovery listener stopped");
  close(sock);
  vTaskDelete(NULL);
}

void probe_link_start_discovery(const char *mac) {
  snprintf(discovery_reply, sizeof(discovery_reply), "STM32-WDEBUG/1 %s", mac);
  if (xTaskCreate(discovery_task, "dap_discovery", 3072, NULL, 4, NULL) !=
      pdPASS)
    ESP_LOGW("probe_link", "Could not start UDP discovery task");
}
