"""Support for Amcrest IP cameras."""
import asyncio
import logging

import voluptuous as vol

from homeassistant.components.camera import (
    Camera, DOMAIN, SUPPORT_ON_OFF, CAMERA_SERVICE_SCHEMA)
from homeassistant.components.ffmpeg import DATA_FFMPEG
from homeassistant.core import callback
from homeassistant.const import (
    ATTR_ENTITY_ID, CONF_NAME, STATE_ON, STATE_OFF)
from homeassistant.helpers.aiohttp_client import (
    async_aiohttp_proxy_stream, async_aiohttp_proxy_web,
    async_get_clientsession)
from homeassistant.helpers.service import extract_entity_ids
try:
    from . import DATA_AMCREST, STREAM_SOURCE_LIST, TIMEOUT
except ImportError:
    from custom_components.amcrest import (
        DATA_AMCREST, STREAM_SOURCE_LIST, TIMEOUT)

DEPENDENCIES = ['amcrest', 'ffmpeg']

_LOGGER = logging.getLogger(__name__)

DATA_AMCREST_CAMS = 'amcrest_cams'

OPTIMISTIC = True

_BOOL_TO_STATE = {True: STATE_ON, False: STATE_OFF}

SERVICE_ENABLE_RECORDING = 'amcrest_enable_recording'
SERVICE_DISABLE_RECORDING = 'amcrest_disable_recording'
SERVICE_GOTO_PRESET = 'amcrest_goto_preset'
SERVICE_SET_COLOR_BW = 'amcrest_set_color_bw'
SERVICE_AUDIO_ON = 'amcrest_audio_on'
SERVICE_AUDIO_OFF = 'amcrest_audio_off'
SERVICE_TOUR_ON = 'amcrest_tour_on'
SERVICE_TOUR_OFF = 'amcrest_tour_off'

ATTR_PRESET = 'preset'
ATTR_COLOR_BW = 'color_bw'

CBW_COLOR = 'color'
CBW_AUTO = 'auto'
CBW_BW = 'bw'
CBW = [CBW_COLOR, CBW_AUTO, CBW_BW]

SERVICE_GOTO_PRESET_SCHEMA = CAMERA_SERVICE_SCHEMA.extend({
    vol.Required(ATTR_PRESET): vol.All(vol.Coerce(int), vol.Range(min=1)),
})
SERVICE_SET_COLOR_BW_SCHEMA = CAMERA_SERVICE_SCHEMA.extend({
    vol.Required(ATTR_COLOR_BW): vol.In(CBW),
})


def _extract_attr(resp, sep='='):
    try:
        return resp.split(sep)[-1].strip()
    except AttributeError:
        return None


async def async_setup_platform(hass, config, async_add_entities,
                               discovery_info=None):
    """Set up an Amcrest IP Camera."""
    # pylint: disable=unused-argument
    # pylint: disable=too-many-statements
    if discovery_info is None:
        return

    device_name = discovery_info[CONF_NAME]
    amcrest = hass.data[DATA_AMCREST][device_name]

    async_add_entities([AmcrestCam(hass, amcrest)], True)

    def target_cameras(service):
        if DATA_AMCREST_CAMS in hass.data:
            if ATTR_ENTITY_ID in service.data:
                entity_ids = extract_entity_ids(hass, service)
            else:
                entity_ids = None
            for camera in hass.data[DATA_AMCREST_CAMS]:
                if entity_ids is None or camera.entity_id in entity_ids:
                    yield camera

    async def async_service_handler(service):
        update_tasks = []
        for camera in target_cameras(service):
            if service.service == SERVICE_ENABLE_RECORDING:
                await camera.async_enable_recording()
            elif service.service == SERVICE_DISABLE_RECORDING:
                await camera.async_disable_recording()
            elif service.service == SERVICE_AUDIO_ON:
                await camera.async_enable_audio()
            elif service.service == SERVICE_AUDIO_OFF:
                await camera.async_disable_audio()
            elif service.service == SERVICE_TOUR_ON:
                await camera.async_tour_on()
            elif service.service == SERVICE_TOUR_OFF:
                await camera.async_tour_off()
            if not camera.should_poll:
                continue
            update_tasks.append(camera.async_update_ha_state(True))
        if update_tasks:
            await asyncio.wait(update_tasks, loop=hass.loop)

    async def async_goto_preset(service):
        preset = service.data.get(ATTR_PRESET)

        update_tasks = []
        for camera in target_cameras(service):
            await camera.async_goto_preset(preset)
            if not camera.should_poll:
                continue
            update_tasks.append(camera.async_update_ha_state(True))
        if update_tasks:
            await asyncio.wait(update_tasks, loop=hass.loop)

    async def async_set_color_bw(service):
        cbw = service.data.get(ATTR_COLOR_BW)

        update_tasks = []
        for camera in target_cameras(service):
            await camera.async_set_color_bw(cbw)
            if not camera.should_poll:
                continue
            update_tasks.append(camera.async_update_ha_state(True))
        if update_tasks:
            await asyncio.wait(update_tasks, loop=hass.loop)

    services = (
        (SERVICE_ENABLE_RECORDING, async_service_handler,
         CAMERA_SERVICE_SCHEMA),
        (SERVICE_DISABLE_RECORDING, async_service_handler,
         CAMERA_SERVICE_SCHEMA),
        (SERVICE_GOTO_PRESET, async_goto_preset, SERVICE_GOTO_PRESET_SCHEMA),
        (SERVICE_SET_COLOR_BW, async_set_color_bw,
         SERVICE_SET_COLOR_BW_SCHEMA),
        (SERVICE_AUDIO_OFF, async_service_handler, CAMERA_SERVICE_SCHEMA),
        (SERVICE_AUDIO_ON, async_service_handler, CAMERA_SERVICE_SCHEMA),
        (SERVICE_TOUR_OFF, async_service_handler, CAMERA_SERVICE_SCHEMA),
        (SERVICE_TOUR_ON, async_service_handler, CAMERA_SERVICE_SCHEMA))
    if not hass.services.has_service(DOMAIN, services[0][0]):
        for service in services:
            hass.services.async_register(DOMAIN, *service)

    return True


class AmcrestCam(Camera):
    """An implementation of an Amcrest IP camera."""

    # pylint: disable=too-many-public-methods
    # pylint: disable=too-many-instance-attributes

    def __init__(self, hass, amcrest):
        """Initialize an Amcrest camera."""
        super(AmcrestCam, self).__init__()
        self._name = amcrest.name
        self._camera = amcrest.device
        self._ffmpeg = hass.data[DATA_FFMPEG]
        self._ffmpeg_arguments = amcrest.ffmpeg_arguments
        self._stream_source = amcrest.stream_source
        self._resolution = amcrest.resolution
        self._token = self._auth = amcrest.authentication
        self._is_recording = False
        self._motion_detection_enabled = None
        self._model = None
        self._static_attrs = {}
        self._audio_enabled = None
        self._color_bw = None
        self._snapshot_lock = asyncio.Lock()

    async def async_added_to_hass(self):
        """Add camera to list."""
        self.hass.data.setdefault(DATA_AMCREST_CAMS, []).append(self)

    async def async_camera_image(self):
        """Return a still image response from the camera."""
        from amcrest import AmcrestError

        if not self.is_on:
            _LOGGER.error(
                'Attempt to take snaphot when %s camera is off', self.name)
            return None
        async with self._snapshot_lock:
            try:
                # Send the request to snap a picture and return raw jpg data
                response = await self.hass.async_add_executor_job(
                    self._camera.snapshot, self._resolution)
                return response.data
            except AmcrestError as error:
                _LOGGER.error(
                    'Could not get image from %s camera due to error: %s',
                    self.name, error)
                return None

    async def handle_async_mjpeg_stream(self, request):
        """Return an MJPEG stream."""
        # The snapshot implementation is handled by the parent class
        if self._stream_source == STREAM_SOURCE_LIST['snapshot']:
            return await super().handle_async_mjpeg_stream(request)

        if self._stream_source == STREAM_SOURCE_LIST['mjpeg']:
            # stream an MJPEG image stream directly from the camera
            websession = async_get_clientsession(self.hass)
            streaming_url = self._camera.mjpeg_url(typeno=self._resolution)
            stream_coro = websession.get(
                streaming_url, auth=self._token, timeout=TIMEOUT)

            return await async_aiohttp_proxy_web(
                self.hass, request, stream_coro)

        # streaming via ffmpeg
        from haffmpeg import CameraMjpeg

        streaming_url = self._camera.rtsp_url(typeno=self._resolution)
        stream = CameraMjpeg(self._ffmpeg.binary, loop=self.hass.loop)
        await stream.open_camera(
            streaming_url, extra_cmd=self._ffmpeg_arguments)

        try:
            return await async_aiohttp_proxy_stream(
                self.hass, request, stream,
                self._ffmpeg.ffmpeg_stream_content_type)
        finally:
            await stream.close()

    # Entity property overrides

    @property
    def should_poll(self):
        """Amcrest camera will be polled only if OPTIMISTIC is False."""
        return not OPTIMISTIC

    @property
    def name(self):
        """Return the name of this camera."""
        return self._name

    @property
    def device_state_attributes(self):
        """Return the Amcrest-spectific camera state attributes."""
        attr = self._static_attrs.copy()
        if self.motion_detection_enabled is not None:
            attr['motion_detection'] = _BOOL_TO_STATE.get(
                self.motion_detection_enabled)
        if self.audio_enabled is not None:
            attr['audio'] = _BOOL_TO_STATE.get(self.audio_enabled)
        if self.color_bw is not None:
            attr[ATTR_COLOR_BW] = self.color_bw
        return attr

    @property
    def assumed_state(self):
        """Return if state is assumed."""
        return OPTIMISTIC

    @property
    def supported_features(self):
        """Flag supported features."""
        return SUPPORT_ON_OFF

    # Camera property overrides

    @property
    def is_recording(self):
        """Return true if the device is recording."""
        return self._is_recording

    @is_recording.setter
    def is_recording(self, enable):
        """Turn recording on or off."""
        from amcrest import AmcrestError

        rec_mode = {'Automatic': 0, 'Manual': 1}
        try:
            self._camera.record_mode = rec_mode[
                'Manual' if enable else 'Automatic']
        except AmcrestError as error:
            _LOGGER.error(
                'Could not %s %s camera recording due to error: %s',
                'enable' if enable else 'disable', self.name, error)
        else:
            if OPTIMISTIC:
                self._is_recording = enable
                self.schedule_update_ha_state()

    @property
    def brand(self):
        """Return the camera brand."""
        return 'Amcrest'

    @property
    def motion_detection_enabled(self):
        """Return the camera motion detection status."""
        return self._motion_detection_enabled

    @motion_detection_enabled.setter
    def motion_detection_enabled(self, enable):
        """Enable or disable motion detection."""
        from amcrest import AmcrestError

        try:
            self._camera.motion_detection = str(enable).lower()
        except AmcrestError as error:
            _LOGGER.error(
                'Could not %s %s camera motion detection due to error: %s',
                'enable' if enable else 'disable', self.name, error)
        else:
            if OPTIMISTIC:
                self._motion_detection_enabled = enable
                self.schedule_update_ha_state()

    @property
    def model(self):
        """Return the camera model."""
        return self._model

    @property
    def frame_interval(self):
        """Return the interval between frames of the mjpeg stream."""
        return 0

    @property
    def stream_source(self):
        """Return the source of the stream."""
        return self._camera.rtsp_url(typeno=self._resolution)

    @property
    def is_on(self):
        """Return true if on."""
        return self.video_enabled

    # Additional Amcrest Camera properties

    @property
    def video_enabled(self):
        """Return the camera video streaming status."""
        return self.is_streaming

    @video_enabled.setter
    def video_enabled(self, enable):
        """Enable or disable camera video stream."""
        from amcrest import AmcrestError

        try:
            self._camera.video_enabled = enable
        except AmcrestError as error:
            _LOGGER.error(
                'Could not %s %s camera video stream due to error: %s',
                'enable' if enable else 'disable', self.name, error)
        else:
            if OPTIMISTIC:
                self.is_streaming = enable
                self.schedule_update_ha_state()

    @property
    def color_bw(self):
        """Return camera color mode."""
        return self._color_bw

    @color_bw.setter
    def color_bw(self, cbw):
        """Set camera color mode."""
        from amcrest import AmcrestError

        try:
            self._camera.day_night_color = CBW.index(cbw)
        except AmcrestError as error:
            _LOGGER.error(
                'Could not set %s camera color mode to %s due to error: %s',
                self.name, cbw, error)
        else:
            if OPTIMISTIC:
                self._color_bw = cbw
                self.schedule_update_ha_state()

    @property
    def audio_enabled(self):
        """Return if audio stream is enabled."""
        return self._audio_enabled

    @audio_enabled.setter
    def audio_enabled(self, enable):
        """Enable or disable audio stream."""
        from amcrest import AmcrestError

        try:
            self._camera.audio_enabled = enable
        except AmcrestError as error:
            _LOGGER.error(
                'Could not %s %s camera audio stream due to error: %s',
                'enable' if enable else 'disable', self.name, error)
        else:
            if OPTIMISTIC:
                self._audio_enabled = enable
                self.schedule_update_ha_state()

    # Other Entity method overrides

    def update(self):
        """Update entity status."""
        from amcrest import AmcrestError

        _LOGGER.debug('Pulling data from %s camera', self.name)
        try:
            if self._model is None:
                self._model = _extract_attr(self._get_cam_attr('device_type'))
            if not self._static_attrs:
                self._update_static_attrs()
            self.is_streaming = self._camera.video_enabled
            self._is_recording = self._camera.record_mode == 'Manual'
            self._motion_detection_enabled = self._camera.is_motion_detector_on()
            self._audio_enabled = self._camera.audio_enabled
            self._color_bw = CBW[self._camera.day_night_color]
        except AmcrestError as error:
            _LOGGER.error(
                'Could not get %s camera attributes due to error: %s',
                self.name, error)

    # Other Camera method overrides

    def turn_off(self):
        """Turn off camera."""
        self.is_recording = False
        self.video_enabled = False

    def turn_on(self):
        """Turn on camera."""
        self.video_enabled = True

    def enable_motion_detection(self):
        """Enable motion detection in the camera."""
        self.motion_detection_enabled = True

    def disable_motion_detection(self):
        """Disable motion detection in camera."""
        self.motion_detection_enabled = False

    # Additional Amcrest Camera service methods

    def enable_recording(self):
        """Enable recording in the camera."""
        self.is_recording = True

    @callback
    def async_enable_recording(self):
        """Call the job and enable recording."""
        return self.hass.async_add_job(self.enable_recording)

    def disable_recording(self):
        """Disable recording in the camera."""
        self.is_recording = False

    @callback
    def async_disable_recording(self):
        """Call the job and disable recording."""
        return self.hass.async_add_job(self.disable_recording)

    def goto_preset(self, preset):
        """Move camera position and zoom to preset."""
        from amcrest import AmcrestError

        try:
            self._camera.go_to_preset(
                    action='start', preset_point_number=preset)
        except AmcrestError as error:
            _LOGGER.error(
                'Could not move %s camera to preset %i due to error: %s',
                self.name, preset, error)

    @callback
    def async_goto_preset(self, preset):
        """Move camera to preset position."""
        return self.hass.async_add_job(self.goto_preset, preset)

    def set_color_bw(self, cbw):
        """Set camera color mode."""
        self.color_bw = cbw

    @callback
    def async_set_color_bw(self, cbw):
        """Set camera color mode."""
        return self.hass.async_add_job(self.set_color_bw, cbw)

    def enable_audio(self):
        """Enable audio."""
        self.audio_enabled = True

    @callback
    def async_enable_audio(self):
        """Enable audio."""
        return self.hass.async_add_job(self.enable_audio)

    def disable_audio(self):
        """Disable audio."""
        self.audio_enabled = False

    @callback
    def async_disable_audio(self):
        """Disable audio."""
        return self.hass.async_add_job(self.disable_audio)

    def tour_on(self):
        """Start camera tour."""
        from amcrest import AmcrestError

        try:
            self._camera.tour(start=True)
        except AmcrestError as error:
            _LOGGER.error(
                'Could not start %s camera tour due to error: %s',
                self.name, error)

    @callback
    def async_tour_on(self):
        """Start camera tour."""
        return self.hass.async_add_job(self.tour_on)

    def tour_off(self):
        """Stop camera tour."""
        from amcrest import AmcrestError

        try:
            self._camera.tour(start=False)
        except AmcrestError as error:
            _LOGGER.error(
                'Could not stop %s camera tour due to error: %s',
                self.name, error)

    @callback
    def async_tour_off(self):
        """Stop camera tour."""
        return self.hass.async_add_job(self.tour_off)

    # Utility methods

    def _get_cam_attr(self, attr):
        from amcrest import AmcrestError

        try:
            return getattr(self._camera, attr)
        except AmcrestError as error:
            _LOGGER.error(
                'Could not get %s camera %s due to error: %s',
                self.name, attr, error)
            return None

    def _update_cam_attr(self, attr):
        value = self._get_cam_attr(attr)
        if value is not None:
            self._static_attrs[attr] = _extract_attr(value)

    def _update_static_attrs(self):
        for attr in ('hardware_version', 'machine_name', 'serial_number'):
            self._update_cam_attr(attr)
        try:
            sw_ver, sw_date = self._get_cam_attr('software_information')
        except TypeError:
            pass
        except ValueError:
            _LOGGER.error(
                'Unexpected %s camera software_information', self.name)
        else:
            self._static_attrs['software_version'] = _extract_attr(sw_ver)
            self._static_attrs['software_build'] = _extract_attr(sw_date, ':')
