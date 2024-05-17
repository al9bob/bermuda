"""
Custom integration to integrate Bermuda BLE Trilateration with Home Assistant.

For more details about this integration, please refer to
https://github.com/agittins/bermuda
"""

from __future__ import annotations

import logging
from datetime import datetime
from datetime import timedelta

import voluptuous as vol
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import MONOTONIC_TIME
from homeassistant.components.bluetooth import BluetoothScannerDevice
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import Config
from homeassistant.core import Event
from homeassistant.core import EventStateChangedData
from homeassistant.core import HomeAssistant
from homeassistant.core import SupportsResponse
from homeassistant.core import callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import area_registry
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import EVENT_DEVICE_REGISTRY_UPDATED
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.helpers.device_registry import EventDeviceRegistryUpdatedData
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import slugify
from homeassistant.util.dt import get_age
from homeassistant.util.dt import now

from custom_components.bermuda.bermuda_device import BermudaDevice
from custom_components.bermuda.bermuda_device_scanner import BermudaDeviceScanner
from custom_components.bermuda.log_spam_less import BermudaLogSpamLess

from .const import ADDR_TYPE_IBEACON
from .const import ADDR_TYPE_PRIVATE_BLE_DEVICE
from .const import BDADDR_TYPE_NOT_MAC48
from .const import BDADDR_TYPE_OTHER
from .const import BDADDR_TYPE_PRIVATE_RESOLVABLE
from .const import BDADDR_TYPE_UNKNOWN
from .const import BEACON_IBEACON_DEVICE
from .const import BEACON_IBEACON_SOURCE
from .const import BEACON_PRIVATE_BLE_DEVICE
from .const import BEACON_PRIVATE_BLE_SOURCE
from .const import CONF_ATTENUATION
from .const import CONF_DEVICES
from .const import CONF_DEVTRACK_TIMEOUT
from .const import CONF_MAX_RADIUS
from .const import CONF_MAX_VELOCITY
from .const import CONF_REF_POWER
from .const import CONF_SMOOTHING_SAMPLES
from .const import CONF_UPDATE_INTERVAL
from .const import CONFDATA_SCANNERS
from .const import DEFAULT_ATTENUATION
from .const import DEFAULT_DEVTRACK_TIMEOUT
from .const import DEFAULT_MAX_RADIUS
from .const import DEFAULT_MAX_VELOCITY
from .const import DEFAULT_REF_POWER
from .const import DEFAULT_SMOOTHING_SAMPLES
from .const import DEFAULT_UPDATE_INTERVAL
from .const import DEVICE_TRACKER
from .const import DOMAIN
from .const import DOMAIN_PRIVATE_BLE_DEVICE
from .const import HIST_KEEP_COUNT
from .const import LOGSPAM_INTERVAL
from .const import PLATFORMS
from .const import PRUNE_MAX_COUNT
from .const import PRUNE_TIME_DEFAULT
from .const import PRUNE_TIME_INTERVAL
from .const import PRUNE_TIME_IRK
from .const import SIGNAL_DEVICE_NEW
from .const import STARTUP_MESSAGE
from .const import UPDATE_INTERVAL

# from typing import TYPE_CHECKING

# from bthome_ble import BTHomeBluetoothDeviceData

# if TYPE_CHECKING:
#     from bleak.backends.device import BLEDevice

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

_LOGGER: logging.Logger = logging.getLogger(__package__)


_LOGGER_SPAM_LESS = BermudaLogSpamLess(_LOGGER, LOGSPAM_INTERVAL)


async def async_setup(
    hass: HomeAssistant, config: Config
):  # pylint: disable=unused-argument;
    """Setting up this integration using YAML is not supported."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up this integration using UI."""
    if hass.data.get(DOMAIN) is None:
        _LOGGER.info(STARTUP_MESSAGE)

    coordinator = hass.data.setdefault(DOMAIN, {})[entry.entry_id] = (
        BermudaDataUpdateCoordinator(hass, entry)
    )

    await coordinator.async_refresh()

    if not coordinator.last_update_success:
        _LOGGER.debug("Coordinator last update failed, rasing ConfigEntryNotReady")
        raise ConfigEntryNotReady

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


class BermudaDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from the Bluetooth component.

    Since we are not actually using an external API and only computing local
    data already gathered by the bluetooth integration, the update process is
    very cheap, and the processing process (currently) rather cheap.

    Future work / algo's etc to keep in mind:

    https://en.wikipedia.org/wiki/Triangle_inequality
    - with distance to two rx nodes, we can apply min and max bounds
      on the distance between them (less than the sum, more than the
      difference). This could allow us to iterively approximate toward
      the rx layout, esp as devices move between (and right up to) rx.
      - bear in mind that rssi errors are typically attenuation-only.
        This means that we should favour *minimum* distances as being
        more accurate, both when weighting measurements from distant
        receivers, and when whittling down a max distance between
        receivers (but beware of the min since that uses differences)

    https://mdpi-res.com/d_attachment/applsci/applsci-10-02003/article_deploy/applsci-10-02003.pdf?version=1584265508
    - lots of good info and ideas.

    TODO / IDEAS:
    - when we get to establishing a fix, we can apply a path-loss factor to
      a calculated vector based on previously measured losses on that path.
      We could perhaps also fine-tune that with real-time measurements from
      fixed beacons to compensate for environmental factors.
    - An "obstruction map" or "radio map" could provide field strength estimates
      at given locations, and/or hint at attenuation by counting "wall crossings"
      for a given vector/path.

    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
    ) -> None:
        """Initialize."""
        self.platforms = []

        self.config_entry = entry

        self.sensor_interval = entry.options.get(
            CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL
        )

        self.stamp_last_prune: float = 0  # When we last pruned device list

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )

        # Track the list of Private BLE devices, noting their entity id
        # and current "last address".
        self.pb_state_sources: dict[str, str | None] = {}

        self.metadevices: dict[str, BermudaDevice] = {}

        @callback
        def handle_state_changes(ev: Event[EventStateChangedData]):
            """Watch for new mac addresses on private ble devices and act."""
            if ev.event_type == EVENT_STATE_CHANGED:
                event_entity = ev.data.get("entity_id", "invalid_event_entity")
                if event_entity in self.pb_state_sources:
                    # It's a state change of an entity we are tracking.
                    new_state = ev.data.get("new_state")
                    if new_state:
                        # _LOGGER.debug("New state change! %s", new_state)
                        # check new_state.attributes.assumed_state
                        if hasattr(new_state, "attributes"):
                            new_address = new_state.attributes.get("current_address")
                            if (
                                new_address is not None
                                and new_address.lower()
                                != self.pb_state_sources[event_entity]
                            ):
                                _LOGGER.debug(
                                    "Have a new source address for %s, %s",
                                    event_entity,
                                    new_address,
                                )
                                self.pb_state_sources[event_entity] = (
                                    new_address.lower()
                                )
                                # Flag that we need new pb checks, and work them out:
                                self._do_private_device_init = True
                                self.update_metadevices()
                                # If no sensors have yet been configured, the coordinator
                                # won't be getting polled for fresh data. Since we have
                                # found something, we should get it to do that.
                                self.hass.add_job(
                                    self.async_config_entry_first_refresh()
                                )

        hass.bus.async_listen(EVENT_STATE_CHANGED, handle_state_changes)

        # First time around we freshen the restored scanner info by
        # forcing a scan of the captured info.
        self._do_full_scanner_init = True

        # First time go through the private ble devices to see if there's
        # any there for us to track.
        self._do_private_device_init = True

        @callback
        def handle_devreg_changes(ev: Event[EventDeviceRegistryUpdatedData]):
            """Update our scanner list if the device registry is changed.

            This catches area changes (on scanners) and any new/changed
            Private BLE Devices."""
            # We could try filtering on "updates" and "area" but I doubt
            # this will fire all that often, and even when it does fire
            # the difference in cycle time appears to be less than 1ms.
            _LOGGER.debug(
                "Device registry has changed, we will reload scanners and Private BLE Devs. ev: %s",
                ev,
            )
            # Mark so that we will rebuild scanner list on next update cycle.
            self._do_full_scanner_init = True
            # Same with Private BLE Device entities
            self._do_private_device_init = True

            # Let's kick off a scanner and private_ble_device scan/refresh/init
            self._refresh_scanners([], self._do_full_scanner_init)
            self.update_metadevices()

            # If there are no `CONFIGURED_DEVICES` and the user only has private_ble_devices
            # in their setup, then we might have done our init runs before that integration
            # was up - in which case we'll get device registry changes. We should kick off
            # the update in case it's not running yet (because of no subscribers yet being
            # attached to the dataupdatecoordinator).
            self.hass.add_job(self._async_update_data())

        # Listen for changes to the device registry and handle them.
        # Primarily for when scanners get moved to a different area,
        # or when Private BLE Device entries are created/loaded.
        hass.bus.async_listen(EVENT_DEVICE_REGISTRY_UPDATED, handle_devreg_changes)

        self.options = {}

        # TODO: This is only here because we haven't set up migration of config
        # entries yet, so some users might not have this defined after an update.
        self.options[CONF_ATTENUATION] = DEFAULT_ATTENUATION
        self.options[CONF_DEVTRACK_TIMEOUT] = DEFAULT_DEVTRACK_TIMEOUT
        self.options[CONF_MAX_RADIUS] = DEFAULT_MAX_RADIUS
        self.options[CONF_MAX_VELOCITY] = DEFAULT_MAX_VELOCITY
        self.options[CONF_REF_POWER] = DEFAULT_REF_POWER
        self.options[CONF_SMOOTHING_SAMPLES] = DEFAULT_SMOOTHING_SAMPLES
        self.options[CONF_UPDATE_INTERVAL] = DEFAULT_UPDATE_INTERVAL

        if hasattr(entry, "options"):
            # Firstly, on some calls (specifically during reload after settings changes)
            # we seem to get called with a non-existant config_entry.
            # Anyway... if we DO have one, convert it to a plain dict so we can
            # serialise it properly when it goes into the device and scanner classes.
            for key, val in entry.options.items():
                if key in (
                    CONF_ATTENUATION,
                    CONF_DEVICES,
                    CONF_DEVTRACK_TIMEOUT,
                    CONF_MAX_RADIUS,
                    CONF_MAX_VELOCITY,
                    CONF_REF_POWER,
                    CONF_SMOOTHING_SAMPLES,
                ):
                    self.options[key] = val

        self.devices: dict[str, BermudaDevice] = {}
        # self.updaters: dict[str, BermudaPBDUCoordinator] = {}

        self.area_reg = area_registry.async_get(hass)

        # Restore the scanners saved in config entry data. We maintain
        # a list of known scanners so we can
        # restore the sensor states even if we don't have a full set of
        # scanner receipts in the discovery data.
        self.scanner_list = []
        if hasattr(entry, "data"):
            for address, saved in entry.data.get(CONFDATA_SCANNERS, {}).items():
                scanner = self._get_or_create_device(address)
                for key, value in saved.items():
                    setattr(scanner, key, value)
                self.scanner_list.append(address)

        hass.services.async_register(
            DOMAIN,
            "dump_devices",
            self.service_dump_devices,
            vol.Schema({vol.Optional("addresses"): cv.string}),
            SupportsResponse.ONLY,
        )

    def sensor_created(self, address):
        """Allows sensor platform to report back that sensors have been set up"""
        dev = self._get_device(address)
        if dev is not None:
            dev.create_sensor_done = True
            _LOGGER.debug("Sensor confirmed created for %s", address)
        else:
            _LOGGER.warning("Very odd, we got sensor_created for non-tracked device")

    def device_tracker_created(self, address):
        """Allows device_tracker platform to report back that sensors have been set up"""
        dev = self._get_device(address)
        if dev is not None:
            dev.create_tracker_done = True
            _LOGGER.debug("Device_tracker confirmed created for %s", address)
        else:
            _LOGGER.warning("Very odd, we got sensor_created for non-tracked device")

    def _get_device(self, address: str) -> BermudaDevice | None:
        """Search for a device entry based on mac address"""
        mac = format_mac(address).lower()
        # format_mac tries to return a lower-cased, colon-separated mac address.
        # failing that, it returns the original unaltered.
        if mac in self.devices:
            return self.devices[mac]
        return None

    def _get_or_create_device(self, address: str) -> BermudaDevice:
        device = self._get_device(address)
        if device is None:
            mac = format_mac(address).lower()
            self.devices[mac] = device = BermudaDevice(
                address=mac, options=self.options
            )
            device.address = mac
            device.unique_id = mac
        return device

    def _clean_charbuf(self, instring: str | None) -> str:
        """Some people writing C on bluetooth devices seem to
        get confused between char arrays, strings and such. This
        function takes a potentially dodgy charbuf from a bluetooth
        device and cleans it of leading/trailing cruft
        and returns what's left, up to the first null, if any.

        If given None it returns an empty string.
        Characters trimmed are space, tab, CR, LF, NUL.
        """
        if instring is not None:
            return instring.strip(" \t\r\n\x00").split("\0")[0]
        return ""

    async def _async_update_data(self):
        """Update data for known devices by scanning bluetooth advert cache.

        This works only with local data, so should be cheap to run
        (no network requests made etc).

        """

        for service_info in bluetooth.async_discovered_service_info(self.hass, False):
            # Note that some of these entries are restored from storage,
            # so we won't necessarily find (immediately, or perhaps ever)
            # scanner entries for any given device.

            # Get/Create a device entry
            device = self._get_or_create_device(service_info.address)

            # BLE MAC addresses (https://www.bluetooth.com/specifications/core54-html/) can
            # be differentiated by the top two MSBs of the 48bit MAC address. At our end at
            # least, this means the first character of the MAC address in aa:bb:cc:dd:ee:ff
            # I have no idea what the distinction between public and random is by bitwise ident,
            # because the random addresstypes cover the entire address-space.
            #
            # - ?? Public
            # - 0b00 (0x00 - 0x3F) Random Private Non-resolvable
            # - 0b01 (0x40 - 0x7F) Random Private Resolvable (ie, IRK devices)
            # - 0x10 (0x80 - 0xBF) ~* Reserved *~ (Is this where ALL Publics live?)
            # - 0x11 (0xC0 - 0xFF) Random Static (may change on power cycle only)
            #
            # What we are really interested in tracking is IRK devices, since they rotate
            # so rapidly (typically )
            #
            # A given device entry (ie, address) won't change, so we only update
            # it once, and also only if it looks like a MAC address
            if device.address_type is BDADDR_TYPE_UNKNOWN:
                if device.address.count(":") != 5:
                    # Doesn't look like an actual MAC address
                    # Mark it as such so we don't spend time testing it again.
                    device.address_type = BDADDR_TYPE_NOT_MAC48
                elif len(device.address) > 0 and device.address[0:1] in "4567":
                    # We're checking if the first char in the address
                    # is one of 4, 5, 6, 7. Python is fun :-)
                    _LOGGER.debug("Identified IRK address on %s", device.address)
                    device.address_type = BDADDR_TYPE_PRIVATE_RESOLVABLE
                else:
                    device.address_type = BDADDR_TYPE_OTHER

            # Check if it's broadcasting an Apple Inc manufacturing data (ID: 0x004C)
            for (
                company_code,
                man_data,
            ) in service_info.advertisement.manufacturer_data.items():
                # if company_code == 0x00E0:  # 224 Google
                #     _LOGGER.debug(
                #         "Found Google Device: %s %s", device.address, man_data.hex()
                #     )
                if company_code == 0x004C:  # 76 Apple Inc
                    # _LOGGER.debug(
                    #     "Found Apple Manufacturer data: %s %s",
                    #     device.address,
                    #     man_data.hex(),
                    # )
                    if man_data[:2] == b"\x02\x15":  # 0x0215:  # iBeacon packet
                        # iBeacon / UUID Support

                        # We provide simplistic support for iBeacons. The
                        # initial/primary use-case is the companion app
                        # for Android phones. We are working with these
                        # assumptions to start with:
                        # - UUID, Major and Minor are static
                        # - MAC address may or may not be static
                        # - We treat a given UUID/Major/Minor combination
                        #   as being unique. If a device sends multiple
                        #   ID's we treat it as *wanting* to be seen as multiple
                        #   devices.

                        # Internally, we still treat the MAC address as the primary
                        # "entity", but if a beacon payload is attached, we
                        # essentially create a duplicate BermudaDevice which uses
                        # the UUID as its "address", and we copy the most recently
                        # received device's details to it. This allows one to decide
                        # to track the MAC address or the UUID.

                        # Combining multiple Minor/Major's into one device isn't
                        # supported at this stage, and I'd suggest doing that sort
                        # of grouping at a higher level (eg using Groups in HA or
                        # matching automations on attributes or a subset of
                        # devices), but if there are prominent use-cases we can
                        # alter our approach.
                        #

                        device.beacon_type.add(BEACON_IBEACON_SOURCE)
                        device.beacon_uuid = man_data[2:18].hex().lower()
                        device.beacon_major = str(
                            int.from_bytes(man_data[18:20], byteorder="big")
                        )
                        device.beacon_minor = str(
                            int.from_bytes(man_data[20:22], byteorder="big")
                        )
                        device.beacon_power = int.from_bytes(
                            [man_data[22]], signed=True
                        )

                        # So, the irony of having major/minor is that the
                        # UniversallyUniqueIDentifier is not even unique
                        # locally, so we need to make one :-)

                        device.beacon_unique_id = f"{device.beacon_uuid}_{device.beacon_major}_{device.beacon_minor}"  # pylint: disable=line-too-long
                        # Note: it's possible that a device sends multiple
                        # beacons. We are only going to process the latest
                        # one in any single update cycle, so we ignore that
                        # possibility for now. Given we re-process completely
                        # each cycle it should *just work*, for the most part.

                        # expose the full id in prefname
                        device.prefname = device.beacon_unique_id

                        # Create a metadevice for this beacon. Metadevices get updated
                        # after all adverts are processed and distances etc are calculated
                        # for the sources.
                        self.register_ibeacon_source(device)

                    else:
                        # apple but not an iBeacon, expose the data in case it's useful.
                        device.prefname = self._clean_charbuf(man_data.hex())
                # else:
                #     _LOGGER.debug(
                #         "Found unknown manufacturer %d data: %s %s",
                #         company_code,
                #         device.address,
                #         man_data.hex(),
                #     )

            # We probably don't need to do all of this every time, but we
            # want to catch any changes, eg when the system learns the local
            # name etc.
            # Clean up names because it seems plenty of bluetooth device creators
            # don't seem to know that buffers !== strings.
            if device.name is None and service_info.device.name:
                device.name = self._clean_charbuf(service_info.device.name)
            if device.local_name is None and service_info.advertisement.local_name:
                device.local_name = self._clean_charbuf(
                    service_info.advertisement.local_name
                )

            device.manufacturer = device.manufacturer or service_info.manufacturer
            device.connectable = service_info.connectable

            # Try to make a nice name for prefname.
            if device.prefname is None or device.prefname.startswith(DOMAIN + "_"):
                device.prefname = (
                    device.name
                    or device.local_name
                    or DOMAIN + "_" + slugify(device.address)
                )

            # Work through the scanner entries...
            matched_scanners = bluetooth.async_scanner_devices_by_address(
                self.hass, service_info.address, False
            )
            for discovered in matched_scanners:
                scanner_device = self._get_device(discovered.scanner.source)
                if scanner_device is None:
                    # The receiver doesn't have a device entry yet, let's refresh
                    # all of them in this batch...
                    self._do_full_scanner_init = True
                    self._do_private_device_init = True
                    self._refresh_scanners(matched_scanners, self._do_full_scanner_init)
                    self._do_full_scanner_init = False
                    scanner_device = self._get_device(discovered.scanner.source)

                if scanner_device is None:
                    # Highly unusual. If we can't find an entry for the scanner
                    # maybe it's from an integration that's not yet loaded, or
                    # perhaps it's an unexpected type that we don't know how to
                    # find.
                    _LOGGER_SPAM_LESS.error(
                        f"missing_scanner_entry_{discovered.scanner.source}",
                        "Failed to find config for scanner %s, this is probably a bug.",
                        discovered.scanner.source,
                    )
                    continue

                # Update the scanner entry on the current device
                device.update_scanner(scanner_device, discovered)

            # END of per-advertisement-by-device loop

        # Scanner entries have been loaded up with latest data, now we can
        # process data for all devices over all scanners.
        for device in self.devices.values():
            # Recalculate smoothed distances, last_seen etc
            device.calculate_data()

        self._refresh_areas_by_min_distance()

        # We might need to freshen deliberately on first start if no new scanners
        # were discovered in the first scan update. This is likely if nothing has changed
        # since the last time we booted.
        if self._do_full_scanner_init:
            if not self._refresh_scanners([], self._do_full_scanner_init):
                _LOGGER.debug(
                    "Failed to refresh scanners, likely config entry not ready."
                )
                # don't fail the update, just try again next time.
                # self.last_update_success = False
            else:
                self._do_full_scanner_init = False

        # set up any beacons and update their data. We do this after all the devices
        # have had their updates done since any beacon inherits data from its source
        # device(s). We do this *before* sensor creation, though.
        self.update_metadevices()

        # The devices are all updated now (and any new scanners and beacons seen have been added),
        # so let's ensure any devices that we create sensors for are set up ready to go.
        # We don't do this sooner because we need to ensure we have every active scanner
        # already loaded up.
        for address, device in self.devices.items():
            if device.create_sensor:
                if not device.create_sensor_done or not device.create_tracker_done:
                    _LOGGER.debug("Firing device_new for %s (%s)", device.name, address)
                    # TODO: Confirm that this call is safe. I think we are always in the event loop
                    # here, so using the async form should be OK. Otherwise use dispatcher_send.
                    async_dispatcher_send(
                        self.hass, SIGNAL_DEVICE_NEW, address, self.scanner_list
                    )

        if self.stamp_last_prune < MONOTONIC_TIME() - PRUNE_TIME_INTERVAL:
            # (periodically) prune any stale device entries...
            self.prune_devices()
            self.stamp_last_prune = MONOTONIC_TIME()

        # end of async update
        self.last_update_success = True

    def prune_devices(self):
        """Scan through all collected devices, and remove those that meet Pruning criteria"""
        prune_list = []
        prunable_stamps = {}

        # build a set of source devices that are still beacon_sources[0]
        metadevice_source_primos = set()
        for metadevice in self.metadevices.values():
            if len(metadevice.beacon_sources) > 0:
                metadevice_source_primos.add(metadevice.beacon_sources[0])

        for device_address, device in self.devices.items():

            # Prune any devices that haven't been heard from for too long, but only
            # if we aren't actively tracking them and it's a traditional MAC address.
            # We just collect the addresses first, and do the pruning after exiting this iterator
            #
            # Reduced selection criteria - basically if if's not:
            # - a scanner (beacuse we need those!)
            # - a private_ble device (because they will re-create anyway, plus we auto-sensor them
            # - create_sensor
            # then it should be up for pruning. A stale iBeacon that we don't actually track
            # should totally be pruned if it's no longer around.
            if (
                device_address not in metadevice_source_primos
                and (not device.create_sensor)  # Not if we track the device
                and (not device.is_scanner)
                and (device.last_seen > 0)  # Don't prune if we haven't initialised yet!
                and device.address_type != BDADDR_TYPE_NOT_MAC48
            ):
                if device.address_type == BDADDR_TYPE_PRIVATE_RESOLVABLE:
                    # This is an IRK source address. We'll *only* want to keep
                    # if if belongs to one of our known Private BLE devices *and*
                    # it's the latest address we have for it.

                    if device.last_seen < MONOTONIC_TIME() - PRUNE_TIME_IRK:
                        _LOGGER.debug(
                            "Marking stale IRK address for pruning: %s",
                            device.name or device_address,
                        )
                        prune_list.append(device_address)
                    else:
                        # It's not stale, but we will prune it if we have to later to fit
                        # into PRUNE_MAX_COUNT
                        prunable_stamps[device_address] = device.last_seen

                elif device.last_seen < MONOTONIC_TIME() - PRUNE_TIME_DEFAULT:
                    # It's a static address, and stale.
                    _LOGGER.debug(
                        "Marking old device entry for pruning: %s",
                        device.name or device_address,
                    )
                    prune_list.append(device_address)
                else:
                    # Device is static, not so old, but we might have to prune it anyway
                    prunable_stamps[device_address] = device.last_seen

        prune_quota = len(self.devices) - len(prune_list) - PRUNE_MAX_COUNT
        if prune_quota > 0:
            # We need to find more addresses to prune. Perhaps we live
            # in a busy train station, or are under some sort of BLE-MAC
            # DOS-attack.
            sorted_addresses = sorted([(v, k) for k, v in prunable_stamps.items()])
            _LOGGER.info("Having to prune %s extra devices to make quota.", prune_quota)
            # pylint: disable-next=unused-variable
            for stamp, address in sorted_addresses[:prune_quota]:
                prune_list.append(address)

        # Perform any pruning we found to do
        for device_address in prune_list:
            _LOGGER.debug("Acting on prune list for %s", device_address)
            del self.devices[device_address]

    def discover_private_ble_metadevices(self):
        """Access the Private BLE Device integration to find metadevices to track

        This function sets up the skeleton metadevice entry for Private BLE (IRK)
        devices, ready for update_metadevices to manage.
        """

        entreg = er.async_get(self.hass)
        devreg = dr.async_get(self.hass)

        if self._do_private_device_init:
            self._do_private_device_init = False
            _LOGGER.debug("Refreshing Private BLE Device list")

            # Iterate through the Private BLE Device integration's entities,
            # and ensure for each "device" we create a source device.
            # pb here means "private ble device"
            pb_entries = self.hass.config_entries.async_entries(
                DOMAIN_PRIVATE_BLE_DEVICE
            )
            for pb_entry in pb_entries:
                pb_entities = entreg.entities.get_entries_for_config_entry_id(
                    pb_entry.entry_id
                )
                # This will be a list of entities for a given private ble device,
                # let's pull out the device_tracker one, since it has the state
                # info we need.
                for pb_entity in pb_entities:

                    if pb_entity.domain == DEVICE_TRACKER:
                        # We found a *device_tracker* entity for the private_ble device.
                        _LOGGER.debug(
                            "Found a Private BLE Device Tracker! %s",
                            pb_entity.entity_id,
                        )

                        # Grab the device entry (for the name, mostly)
                        if pb_entity.device_id is not None:
                            pb_device = devreg.async_get(pb_entity.device_id)
                        else:
                            pb_device = None

                        # Grab the current state (so we can access the source address attrib)
                        pb_state = self.hass.states.get(pb_entity.entity_id)

                        if pb_state:  # in case it's not there yet
                            pb_source_address = pb_state.attributes.get(
                                "current_address", None
                            )
                        else:
                            # Private BLE Device hasn't yet found a source device
                            pb_source_address = None

                        # Get the IRK of the device, which we will use as the address
                        # for the metadevice.
                        # As of 2024.4.0b4 Private_ble appends _device_tracker to the
                        # unique_id of the entity, while we really want to know
                        # the actual IRK, so handle either case by splitting it:
                        _irk = pb_entity.unique_id.split("_")[0]

                        # Create our Meta-Device and tag it up...
                        metadevice = self._get_or_create_device(_irk)
                        # Since user has already configured the Private BLE Device, we
                        # always create sensors for them.
                        metadevice.create_sensor = True
                        metadevice.beacon_type.add(BEACON_PRIVATE_BLE_DEVICE)
                        metadevice.address_type = ADDR_TYPE_PRIVATE_BLE_DEVICE

                        # Set a nice name
                        metadevice.name = getattr(
                            pb_device, "name_by_user", getattr(pb_device, "name")
                        )
                        metadevice.prefname = metadevice.name

                        # Ensure we track this PB entity so we get source address updates.
                        if pb_entity.entity_id not in self.pb_state_sources:
                            self.pb_state_sources[pb_entity.entity_id] = None

                        metadevice.beacon_unique_id = _irk

                        # Add metadevice to list so it gets included in update_metadevices
                        if metadevice.address not in self.metadevices:
                            self.metadevices[metadevice.address] = metadevice

                        if pb_source_address is not None:
                            # We've got a source MAC address!
                            pb_source_address = pb_source_address.lower()

                            # Set up and tag the source device entry
                            source_device = self._get_or_create_device(
                                pb_source_address
                            )
                            source_device.beacon_type.add(BEACON_PRIVATE_BLE_SOURCE)

                            # This should always be the latest known source address,
                            # since private ble device tells us so.
                            # So ensure it's listed, and listed first.
                            if (
                                len(metadevice.beacon_sources) == 0
                                or metadevice.beacon_sources[0] != pb_source_address
                            ):
                                metadevice.beacon_sources.insert(0, pb_source_address)

                            # Update state_sources so we can track when it changes
                            self.pb_state_sources[pb_entity.entity_id] = (
                                pb_source_address
                            )

                        else:
                            _LOGGER.debug(
                                "No address available for PB Device %s",
                                pb_entity.entity_id,
                            )

    def register_ibeacon_source(self, source_device: BermudaDevice):
        """Create or update the meta-device for tracking an iBeacon.

        This should be called each time we discover a new address advertising
        an iBeacon. This might happen only once at startup, but will also
        happen each time a new MAC address is used by a given iBeacon.

        This does not update the beacon's details (distance etc), that is done
        in the update_metadevices function after all data has been gathered."""

        if BEACON_IBEACON_SOURCE not in source_device.beacon_type:
            _LOGGER.error(
                "Only IBEACON_SOURCE devices can be used to see a beacon metadevice. %s is not.",
                source_device.name,
            )
        if source_device.beacon_unique_id is None:
            _LOGGER.error(
                "Source device %s is not a valid iBeacon!", source_device.name
            )
        else:

            metadevice = self._get_device(source_device.beacon_unique_id)
            if metadevice is None:

                # #### NEW METADEVICE #####
                # (do one-off init stuff here)

                metadevice = self._get_or_create_device(source_device.beacon_unique_id)
                if metadevice.address not in self.metadevices:
                    self.metadevices[metadevice.address] = metadevice
                else:
                    _LOGGER.warning(
                        "Metadevice already tracked despite not existing yet. %s",
                        metadevice.address,
                    )

                metadevice.address_type = ADDR_TYPE_IBEACON
                metadevice.beacon_type.add(BEACON_IBEACON_DEVICE)
                # Copy over the beacon attributes
                for attribute in (
                    "beacon_unique_id",
                    "beacon_uuid",
                    "beacon_major",
                    "beacon_minor",
                    "beacon_power",
                ):
                    setattr(
                        metadevice, attribute, getattr(source_device, attribute, None)
                    )

                # Check if we should set up sensors for this beacon
                if metadevice.address.upper() in self.options.get(CONF_DEVICES, []):
                    # This is a meta-device we track. Flag it for set-up:
                    metadevice.create_sensor = True

            # #### EXISTING METADEVICE ####
            # (only do things that might have to change when MAC address cycles etc)

            if source_device.address not in metadevice.beacon_sources:
                # We have a *new* source device.
                # insert this device as a known source
                metadevice.beacon_sources.insert(0, source_device.address)
                # and trim the list of sources
                del metadevice.beacon_sources[HIST_KEEP_COUNT:]

    def update_metadevices(self):
        """Create or update iBeacon, Private_BLE and other meta-devices from
        the received advertisements.

        Note that at this point all the distances etc should be fresh for
        the source devices, so we can just copy values from them to the metadevice.
        """

        # First seed the metadevice skeletons and set their latest beacon_source entries
        # Private BLE Devices:
        self.discover_private_ble_metadevices()

        # iBeacon devices should already have their metadevices created.

        for metadev in self.metadevices.values():

            # We Expect the first beacon source to be the current one.
            # This is maintained by ibeacon or private_ble metadevice creation/update
            latest_source: str | None = None
            source_device: BermudaDevice | None = None
            if len(metadev.beacon_sources) > 0:
                latest_source = metadev.beacon_sources[0]
                if latest_source is not None:
                    source_device = self._get_device(latest_source)

            if latest_source is not None and source_device is not None:
                # Map the source device's scanner list into ours
                metadev.scanners = source_device.scanners

                # anything that isn't already set to something interesting, overwrite
                # it with the new device's data.
                # Defaults:
                for attribute in [
                    # "create_sensor",  # don't copy this, maybe we're tracking the device alone
                    "local_name",  # names we copy if there isn't one already.
                    "manufacturer",
                    "name",
                    # "options",
                    "prefname",
                ]:
                    if hasattr(metadev, attribute):
                        if getattr(metadev, attribute) in [None, False]:
                            setattr(
                                metadev, attribute, getattr(source_device, attribute)
                            )
                    else:
                        _LOGGER.error(
                            "Devices don't have a '%s' attribute, this is a bug.",
                            attribute,
                        )
                # Anything that's VERY interesting, overwrite it regardless of what's already there:
                # INTERESTING:
                for attribute in [
                    "area_distance",
                    "area_id",
                    "area_name",
                    "area_rssi",
                    "area_scanner",
                    "beacon_major",
                    "beacon_minor",
                    "beacon_power",
                    "beacon_unique_id",
                    "beacon_uuid",
                    "connectable",
                    "zone",
                ]:
                    if hasattr(metadev, attribute):
                        setattr(metadev, attribute, getattr(source_device, attribute))
                    else:
                        _LOGGER.error(
                            "Devices don't have a '%s' attribute, this is a bug.",
                            attribute,
                        )

                if source_device.last_seen > metadev.last_seen:
                    # Source is newer than the latest recorded, update last_seen
                    metadev.last_seen = source_device.last_seen

                elif source_device.last_seen == 0:
                    # _LOGGER.debug(
                    #     "New source %s for %s has no stamp yet. This is"
                    #     " expected if it's a fresh Private BLE source.",
                    #     source_device.address,
                    #     metadev.name
                    # )
                    pass
                elif source_device.last_seen < metadev.last_seen:
                    # We should not have a source device that is older than the
                    # current metadevice, so flag this if it occurs.
                    # This caught bug #138, not that I realised it at the time!
                    # (https://github.com/agittins/bermuda/issues/138)
                    _LOGGER.debug(
                        "Using freshest advert from %s for %s but it's still %s seconds too old!",
                        source_device.address,
                        metadev.name,
                        metadev.last_seen - source_device.last_seen,
                    )
                # else the stamps are equal, which is perfectly OK.

    def dt_mono_to_datetime(self, stamp) -> datetime:
        """Given a monotonic timestamp, convert to datetime object"""
        age = MONOTONIC_TIME() - stamp
        return now() - timedelta(seconds=age)

    def dt_mono_to_age(self, stamp) -> str:
        """Convert monotonic timestamp to age (eg: "6 seconds ago")"""
        return get_age(self.dt_mono_to_datetime(stamp))

    def _refresh_areas_by_min_distance(self):
        """Set area for ALL devices based on closest beacon"""
        for device in self.devices.values():
            if device.is_scanner is not True:
                self._refresh_area_by_min_distance(device)

    def _refresh_area_by_min_distance(self, device: BermudaDevice):
        """Very basic Area setting by finding closest beacon to a given device"""
        assert device.is_scanner is not True
        closest_scanner: BermudaDeviceScanner | None = None

        for scanner in device.scanners.values():
            # Check each scanner and keep note of the closest one based on rssi_distance.
            # Note that rssi_distance is smoothed/filtered, and might be None if the last
            # reading was old enough that our algo decides it's "away".
            if (
                scanner.rssi_distance is not None
                and scanner.rssi_distance
                < self.options.get(CONF_MAX_RADIUS, DEFAULT_MAX_RADIUS)
            ):
                # It's inside max_radius...
                if closest_scanner is None:
                    # no encumbent, we win!
                    closest_scanner = scanner
                else:
                    if (
                        closest_scanner.rssi_distance is None
                        or scanner.rssi_distance < closest_scanner.rssi_distance
                    ):
                        # We're closer than the last-closest, we win!
                        closest_scanner = scanner

        if closest_scanner is not None:
            # We found a winner
            old_area = device.area_name
            device.area_id = closest_scanner.area_id
            areas = self.area_reg.async_get_area(device.area_id)
            if hasattr(areas, "name"):
                device.area_name = getattr(areas, "name", "invalid_area")
            else:
                # Wasn't a single area entry. Let's freak out, but not in a spammy way.
                _LOGGER_SPAM_LESS.warning(
                    f"scanner_no_area_{closest_scanner.name}",
                    "Could not discern area from scanner %s: %s."
                    "Please assign an area then reload this integration"
                    "- Bermuda can't really work without it.",
                    closest_scanner.name,
                    areas,
                )
                device.area_name = f"No area: {closest_scanner.name}"
            device.area_distance = closest_scanner.rssi_distance
            device.area_rssi = closest_scanner.rssi
            device.area_scanner = closest_scanner.name
            if (old_area != device.area_name) and device.create_sensor:
                # We check against area_name so we can know if the
                # device's area changed names.
                _LOGGER.debug(
                    "Device %s was in '%s', now in '%s'",
                    device.name,
                    old_area,
                    device.area_name,
                )
        else:
            # Not close to any scanners!
            device.area_id = None
            device.area_name = None
            device.area_distance = None
            device.area_rssi = None
            device.area_scanner = None

    def _refresh_scanners(
        self, scanners: list[BluetoothScannerDevice], do_full_scan=False
    ):
        """Refresh our local (and saved) list of scanners (BLE Proxies)"""
        addresses = set()
        update_scannerlist = False

        for scanner in scanners:
            addresses.add(scanner.scanner.source.lower())

        # If we are doing a full scan, add all the known
        # scanner addresses to the list, since that will cover
        # the scanners that have been restored from config.data
        if do_full_scan:
            update_scannerlist = True
            for address in self.scanner_list:
                addresses.add(address.lower())

        if len(addresses) > 0:
            # FIXME: Really? This can't possibly be a sensible nesting of loops.
            # should probably look at the API. Anyway, we are checking any devices
            # that have a "mac" or "bluetooth" connection,
            for dev_entry in self.hass.data["device_registry"].devices.data.values():
                for dev_connection in dev_entry.connections:
                    if dev_connection[0] in ["mac", "bluetooth"]:
                        found_address = format_mac(dev_connection[1])
                        if found_address in addresses:
                            scandev = self._get_device(found_address)
                            if scandev is None:
                                # It's a new scanner, we will need to update our saved config.
                                _LOGGER.debug("New Scanner: %s", found_address)
                                update_scannerlist = True
                                scandev = self._get_or_create_device(found_address)
                            # Found the device entry and have created our scannerdevice,
                            # now update any fields that might be new from the device reg:
                            scandev_orig = scandev
                            scandev.area_id = dev_entry.area_id
                            scandev.entry_id = dev_entry.id
                            if dev_entry.name_by_user is not None:
                                scandev.name = dev_entry.name_by_user
                            else:
                                scandev.name = dev_entry.name
                            areas = self.area_reg.async_get_area(dev_entry.area_id)
                            if areas is not None and hasattr(areas, "name"):
                                scandev.area_name = areas.name
                            else:
                                _LOGGER_SPAM_LESS.warning(
                                    f"no_area_on_update{scandev.name}",
                                    "No area name or no area id updating scanner %s, area_id %s",
                                    scandev.name,
                                    areas,
                                )
                            scandev.is_scanner = True
                            # If the scanner data we loaded from our saved data appears
                            # out of date, trigger a full rescan of seen scanners.
                            if scandev_orig != scandev:
                                # something changed, let's update the saved list.
                                _LOGGER.debug(
                                    "Scanner info for %s has changed, we'll update our saved data.",
                                    scandev.name,
                                )
                                update_scannerlist = True
        if update_scannerlist:
            # Take the existing list of scanners and save them into config data
            # for our next start-up.
            for entry in self.hass.config_entries.async_entries(
                DOMAIN, include_disabled=False, include_ignore=False
            ):
                _LOGGER.debug("Loaded entry %s", entry.entry_id)
                self.config_entry = entry
            self.scanner_list = []
            confdata_scanners: dict[str, dict] = {}
            for device in self.devices.values():
                if device.is_scanner:
                    confdata_scanners[device.address] = device.to_dict()
                    self.scanner_list.append(device.address)

            if self.config_entry is None:
                _LOGGER.debug(
                    "Aborting refresh scanners due to config entry not being ready"
                )
                return False

            _LOGGER.debug(
                "Replacing config data scanners was %s now %s",
                self.config_entry.data.get(CONFDATA_SCANNERS, {}),
                confdata_scanners,
            )

            @callback
            def async_call_update_entry() -> None:
                """Called in the event loop to update the scanner entries in our config

                We do this via add_job to ensure it runs in the event loop.
                """
                self.hass.config_entries.async_update_entry(
                    self.config_entry,
                    data={
                        **self.config_entry.data,
                        CONFDATA_SCANNERS: confdata_scanners,
                    },
                )

            self.hass.add_job(async_call_update_entry)

        return True

    async def service_dump_devices(self, call):  # pylint: disable=unused-argument;
        """Return a dump of beacon advertisements by receiver"""
        out = {}
        addresses_input = call.data.get("addresses", "")
        if addresses_input != "":
            addresses = addresses_input.upper().split()
        else:
            addresses = []
        for address, device in self.devices.items():
            if len(addresses) == 0 or address.upper() in addresses:
                out[address] = device.to_dict()
        return out


async def async_remove_config_entry_device(
    hass: HomeAssistant, config_entry: ConfigEntry, device_entry: DeviceEntry
) -> bool:
    """Remove a config entry from a device."""
    coordinator: BermudaDataUpdateCoordinator = hass.data[DOMAIN][config_entry.entry_id]
    address = None
    for ident in device_entry.identifiers:
        try:
            if ident[0] == DOMAIN:
                # the identifier should be the base device address, and
                # may have "_range" or some other per-sensor suffix.
                # The address might be a mac address, IRK or iBeacon uuid
                address = ident[1].split("_")[0]
        except KeyError:
            pass
    if address is not None:
        try:
            coordinator.devices[format_mac(address)].create_sensor = False
        except KeyError:
            _LOGGER.warning("Failed to locate device entry for %s", address)
        return True
    # Even if we don't know this address it probably just means it's stale or from
    # a previous version that used weirder names. Allow it.
    _LOGGER.warning(
        "Didn't find address for %s but allowing deletion to proceed.",
        device_entry.name,
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Handle removal of an entry."""
    if unload_result := await hass.config_entries.async_unload_platforms(
        entry, PLATFORMS
    ):
        _LOGGER.debug("Unloaded platforms.")
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_result


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry."""
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)
