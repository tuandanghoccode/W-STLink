#include <stdio.h>
#include <string.h>
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "host/ble_hs.h"
#include "host/ble_hs_mbuf.h"
#include "host/ble_store.h"
#include "nimble/nimble_port.h"
#include "nimble/nimble_port_freertos.h"
#include "services/gap/ble_svc_gap.h"
#include "services/gatt/ble_svc_gatt.h"

#include "DAP_config.h"
#include "DAP.h"
#include "cmsis_dap_ble.h"

#define FRAME_HEADER_SIZE 8
#define NO_CONNECTION BLE_HS_CONN_HANDLE_NONE
#define PROBE_UUID(id) BLE_UUID128_INIT(0xd0, 0x8d, 0x1a, 0x8c, 0x7a, 0x32, \
        0xc1, 0xa4, 0x26, 0x4b, 0xc9, 0x58, id, 0x00, 0x3f, 0x8f)

static const char tag[] = "cmsis_dap_ble";
static const ble_uuid128_t service_uuid = PROBE_UUID(1);
static const ble_uuid128_t rx_uuid = PROBE_UUID(2);
static const ble_uuid128_t tx_uuid = PROBE_UUID(3);
static uint16_t tx_handle;
static uint8_t address_type;
static char device_name[sizeof("STM32-WDEBUG-000000")];
static SemaphoreHandle_t lock;
static TaskHandle_t worker;

// Callbacks assemble a frame. Only the worker executes DAP and drives SWD.
static struct {
    uint16_t connection;
    uint32_t generation;
    bool subscribed;
    bool busy;
    size_t received;
    size_t expected;
    uint8_t bytes[FRAME_HEADER_SIZE + DAP_PACKET_SIZE];
} link = {.connection = NO_CONNECTION};

void ble_store_config_init(void);

static int receive_chunk(uint16_t connection, struct os_mbuf *buffer)
{
    uint16_t length = OS_MBUF_PKTLEN(buffer);
    uint8_t *bytes = link.bytes;
    size_t total = link.received + length;

    if (connection != link.connection || !link.subscribed)
        return BLE_ATT_ERR_WRITE_NOT_PERMITTED;
    if (link.busy)
        return BLE_ATT_ERR_INSUFFICIENT_RES;
    if (!length || total > sizeof(link.bytes) ||
        os_mbuf_copydata(buffer, 0, length, bytes + link.received) != 0)
        goto invalid_frame;

    if (total >= FRAME_HEADER_SIZE && link.expected == 0) {
        size_t payload_length = bytes[4] | ((size_t)bytes[5] << 8);
        if (memcmp(bytes, "DAP\0", 4) != 0 || bytes[6] != 1 || bytes[7] != 0 ||
            payload_length == 0 || payload_length > DAP_PACKET_SIZE)
            goto invalid_frame;
        link.expected = FRAME_HEADER_SIZE + payload_length;
    }
    if (link.expected != 0 && total > link.expected)
        goto invalid_frame;

    link.received = total;
    if (total == link.expected) {
        link.busy = true;
        xTaskNotifyGive(worker);
    }
    return 0;

invalid_frame:
    link.received = 0;
    link.expected = 0;
    return BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN;
}

static int gatt_access(uint16_t connection, uint16_t attribute,
                       struct ble_gatt_access_ctxt *context, void *unused)
{
    if (context->op != BLE_GATT_ACCESS_OP_WRITE_CHR || attribute == tx_handle)
        return BLE_ATT_ERR_WRITE_NOT_PERMITTED;
    xSemaphoreTake(lock, portMAX_DELAY);
    int error = receive_chunk(connection, context->om);
    xSemaphoreGive(lock);
    return error;
}

static const struct ble_gatt_svc_def services[] = {
    {
        .type = BLE_GATT_SVC_TYPE_PRIMARY,
        .uuid = &service_uuid.u,
        .characteristics = (const struct ble_gatt_chr_def[]) {
            {
                .uuid = &rx_uuid.u,
                .access_cb = gatt_access,
                .flags = BLE_GATT_CHR_F_WRITE | BLE_GATT_CHR_F_WRITE_ENC,
            },
            {
                .uuid = &tx_uuid.u,
                .access_cb = gatt_access,
                .flags = BLE_GATT_CHR_F_NOTIFY,
                .val_handle = &tx_handle,
            },
            {0},
        },
    },
    {0},
};

static int gap_event(struct ble_gap_event *event, void *unused);

static void advertise(void)
{
    const struct ble_hs_adv_fields advertisement = {
        .flags = BLE_HS_ADV_F_DISC_GEN | BLE_HS_ADV_F_BREDR_UNSUP,
        .uuids128 = &service_uuid,
        .num_uuids128 = 1,
        .uuids128_is_complete = 1,
    };
    const struct ble_hs_adv_fields scan_response = {
        .name = (const uint8_t *)device_name,
        .name_len = strlen(device_name),
        .name_is_complete = 1,
    };
    const struct ble_gap_adv_params params = {
        .conn_mode = BLE_GAP_CONN_MODE_UND,
        .disc_mode = BLE_GAP_DISC_MODE_GEN,
    };
    int rc = ble_gap_adv_set_fields(&advertisement);
    if (rc == 0)
        rc = ble_gap_adv_rsp_set_fields(&scan_response);
    if (rc == 0)
        rc = ble_gap_adv_start(address_type, NULL, BLE_HS_FOREVER, &params, gap_event, NULL);
    if (rc != 0)
        ESP_LOGE(tag, "Could not advertise: %d", rc);
}

static void change_connection(uint16_t connection)
{
    xSemaphoreTake(lock, portMAX_DELAY);
    link.generation++;
    link.connection = connection;
    link.subscribed = false;
    link.busy = false;
    link.received = 0;
    link.expected = 0;
    xSemaphoreGive(lock);
    xTaskNotifyGive(worker);
}

static int gap_event(struct ble_gap_event *event, void *unused)
{
    switch (event->type) {
    case BLE_GAP_EVENT_CONNECT:
        if (event->connect.status == 0)
            change_connection(event->connect.conn_handle);
        else
            advertise();
        break;
    case BLE_GAP_EVENT_DISCONNECT:
        change_connection(NO_CONNECTION);
        advertise();
        break;
    case BLE_GAP_EVENT_SUBSCRIBE:
        if (event->subscribe.attr_handle != tx_handle)
            break;
        xSemaphoreTake(lock, portMAX_DELAY);
        if (event->subscribe.conn_handle == link.connection) {
            link.subscribed = event->subscribe.cur_notify;
            if (!link.subscribed) {
                link.generation++;
                link.busy = false;
                link.received = 0;
                link.expected = 0;
                xTaskNotifyGive(worker);
            }
        }
        xSemaphoreGive(lock);
        break;
    case BLE_GAP_EVENT_ADV_COMPLETE:
        advertise();
        break;
    }
    return 0;
}

static bool session_current(uint32_t generation, bool release_request)
{
    xSemaphoreTake(lock, portMAX_DELAY);
    bool current = generation == link.generation && link.subscribed;
    if (current && release_request)
        link.busy = false;
    xSemaphoreGive(lock);
    return current;
}

static bool send_frame(uint16_t connection, uint32_t generation,
                       const uint8_t *frame, size_t length)
{
    uint16_t mtu = ble_att_mtu(connection);
    if (mtu <= 3)
        return false;
    size_t maximum = mtu - 3;
    for (size_t offset = 0; offset < length;) {
        size_t chunk = length - offset;
        if (chunk > maximum)
            chunk = maximum;
        bool last = offset + chunk == length;
        // Allow the next write once the previous frame can have arrived.
        // The worker's response buffer is separate from the receive buffer.
        int rc = BLE_HS_ENOMEM;
        for (int retry = 0; retry < 50 && (rc == BLE_HS_ENOMEM || rc == BLE_HS_EBUSY); retry++) {
            if (!session_current(generation, last))
                return false;
            struct os_mbuf *data = ble_hs_mbuf_from_flat(frame + offset, chunk);
            rc = data ? ble_gatts_notify_custom(connection, tx_handle, data) : BLE_HS_ENOMEM;
            if (rc == BLE_HS_ENOMEM || rc == BLE_HS_EBUSY)
                vTaskDelay(1);
        }
        if (rc != 0)
            return false;
        offset += chunk;
    }
    return true;
}

static size_t process_request(const uint8_t *request, size_t length, uint8_t *response)
{
    if (request[0] == ID_DAP_TransferBlock) {
        // The shared processor trusts the block count supplied by its caller.
        unsigned int count = request[2] | ((unsigned int)request[3] << 8);
        size_t data_length = count * 4;
        bool read = (request[4] & DAP_TRANSFER_RnW) != 0;
        if (length < 5 || (read ? data_length + 4 > DAP_PACKET_SIZE
                                : data_length + 5 > length)) {
            response[0] = ID_DAP_Invalid;
            return 1;
        }
    }
    return DAP_ProcessCommand(request, response) & 0xffff;
}

static void dap_task(void *gpio)
{
    // The existing DAP parser reads fields directly from fixed-size storage.
    // Keep sufficient zero padding for short SWD sequence and transfer packets.
    static uint8_t request[(DAP_PACKET_SIZE > 4096) ? DAP_PACKET_SIZE : 4096];
    static uint8_t response[FRAME_HEADER_SIZE + ((DAP_PACKET_SIZE > 4096) ? DAP_PACKET_SIZE : 4096)];
    static DAP_Data_t data;
    uint32_t active_generation = 0;

    cmsis_dap_gpio_config = gpio;
    DAP_Data = &data;
    DAP_Setup();
    for (;;) {
        ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
        xSemaphoreTake(lock, portMAX_DELAY);
        uint16_t connection = link.connection;
        uint32_t generation = link.generation;
        size_t length = link.expected && link.received == link.expected
                        ? link.expected - FRAME_HEADER_SIZE : 0;
        if (length) {
            memcpy(request, link.bytes + FRAME_HEADER_SIZE, length);
            memset(request + length, 0, sizeof(request) - length);
            link.received = 0;
            link.expected = 0;
        }
        xSemaphoreGive(lock);

        if (active_generation != generation) {
            uint8_t disconnect = ID_DAP_Disconnect;
            uint8_t reply[2];
            DAP_ProcessCommand(&disconnect, reply);
            active_generation = generation;
        }
        if (!length || !session_current(generation, false))
            continue;

        size_t response_length = process_request(request, length, response + FRAME_HEADER_SIZE);
        memcpy(response, "DAP\0", 4);
        response[4] = response_length;
        response[5] = response_length >> 8;
        response[6] = 2;
        response[7] = 0;
        if (response_length > DAP_PACKET_SIZE ||
            !send_frame(connection, generation, response, FRAME_HEADER_SIZE + response_length)) {
            xSemaphoreTake(lock, portMAX_DELAY);
            bool connected = generation == link.generation;
            xSemaphoreGive(lock);
            if (connected)
                ble_gap_terminate(connection, BLE_ERR_REM_USER_CONN_TERM);
        }
    }
}

static void host_sync(void)
{
    int rc = ble_hs_id_infer_auto(0, &address_type);
    if (rc == 0)
        advertise();
    else
        ESP_LOGE(tag, "Could not find BLE address: %d", rc);
}

static void host_reset(int reason)
{
    ESP_LOGW(tag, "NimBLE reset: %d", reason);
    change_connection(NO_CONNECTION);
}

static void host_task(void *unused)
{
    nimble_port_run();
    nimble_port_freertos_deinit();
}

esp_err_t cmsis_dap_ble_start(const struct cmsis_dap_gpio_config *gpio, const char *mac)
{
    snprintf(device_name, sizeof(device_name), "STM32-WDEBUG-%s", mac + 6);
    esp_err_t err = nimble_port_init();
    if (err != ESP_OK)
        return err;

    ble_hs_cfg.sync_cb = host_sync;
    ble_hs_cfg.reset_cb = host_reset;
    ble_hs_cfg.sm_io_cap = BLE_SM_IO_CAP_NO_IO;
    ble_hs_cfg.sm_bonding = 1;
    ble_hs_cfg.sm_sc = 1;
    ble_hs_cfg.sm_our_key_dist = BLE_SM_PAIR_KEY_DIST_ENC | BLE_SM_PAIR_KEY_DIST_ID;
    ble_hs_cfg.sm_their_key_dist = BLE_SM_PAIR_KEY_DIST_ENC | BLE_SM_PAIR_KEY_DIST_ID;
    ble_hs_cfg.store_status_cb = ble_store_util_status_rr;
    ble_store_config_init();

    ble_svc_gap_init();
    ble_svc_gatt_init();
    if (ble_att_set_preferred_mtu(247) != 0 ||
        ble_svc_gap_device_name_set(device_name) != 0 ||
        ble_gatts_count_cfg(services) != 0 ||
        ble_gatts_add_svcs(services) != 0) {
        err = ESP_FAIL;
        goto fail;
    }
    lock = xSemaphoreCreateMutex();
    if (!lock) {
        err = ESP_ERR_NO_MEM;
        goto fail;
    }
    if (xTaskCreate(dap_task, "dap_ble", 4096, (void *)gpio, 5, &worker) != pdPASS) {
        vSemaphoreDelete(lock);
        err = ESP_ERR_NO_MEM;
        goto fail;
    }
    nimble_port_freertos_init(host_task);
    ESP_LOGI(tag, "BLE probe ready as %s", device_name);
    return ESP_OK;

fail:
    nimble_port_deinit();
    return err;
}
