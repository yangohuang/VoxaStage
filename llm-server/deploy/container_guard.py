"""Continuous fail-closed occupancy guard for an isolated deployment trial.

The host supplies a bounded health probe and an identity-scoped abort callback.
Probe calls are serialized. An interval is a delay between completed probes,
not a guarantee that slow health endpoints are detected within 200ms.
This module does not choose or stop Docker containers by itself.
"""
import json
import re
import threading


class OccupancyViolation(RuntimeError):
    pass


class OwnedContainer:
    """Serialize cleanup and verify ownership immediately before each mutation.

    invoke must run an argument list without a shell, raise on failure, and
    enforce a timeout. Only successful stops permit removal; neither operation
    can select a container by a mutable name or wildcard.
    """
    def __init__(self, container_id, image_id, run_id, invoke):
        if (not isinstance(container_id, str) or not re.fullmatch('[0-9a-f]{64}', container_id)
                or not isinstance(image_id, str) or not re.fullmatch('sha256:[0-9a-f]{64}', image_id)
                or not isinstance(run_id, str) or not run_id.strip()):
            raise ValueError('invalid_container_ownership')
        self.container_id = container_id
        self.image_id = image_id
        self.run_id = run_id
        self.invoke = invoke
        self._lock = threading.Lock()
        self._stopped = False

    def _verify(self):
        metadata = json.loads(self.invoke(['docker', 'container', 'inspect', self.container_id]))
        if (len(metadata) != 1 or metadata[0].get('Id') != self.container_id
                or metadata[0].get('Image') != self.image_id
                or metadata[0].get('Config', {}).get('Labels', {}).get(
                    'org.voxastage.validation-run') != self.run_id):
            raise RuntimeError('container_identity_mismatch')

    def stop(self):
        with self._lock:
            if self._stopped:
                return
            self._verify()
            self.invoke(['docker', 'stop', '--time', '5', self.container_id])
            self._stopped = True

    def remove(self):
        with self._lock:
            if not self._stopped:
                raise RuntimeError('container_not_stopped')
            self._verify()
            self.invoke(['docker', 'rm', self.container_id])


def validate_idle_health(application, model):
    if application.get('ready') is not True or model.get('ready') is not True:
        raise OccupancyViolation('daily_service_not_ready')
    sessions = application.get('active_sessions')
    if type(sessions) is not int or sessions != 0:
        raise OccupancyViolation('application_busy_or_unknown')
    if model.get('busy') is not False:
        raise OccupancyViolation('model_busy_or_unknown')


class OccupancyGuard:
    def __init__(self, probe, abort, interval=.2):
        self.probe = probe
        self.abort = abort
        self.interval = interval
        self._failure = None
        self.abort_error = None
        self._probe_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._monitor, daemon=True)

    @property
    def running(self):
        return self._thread.is_alive()

    def raise_if_failed(self):
        if self._failure is not None:
            raise OccupancyViolation(str(self._failure)) from self._failure

    def check_now(self):
        with self._probe_lock:
            self.raise_if_failed()
            try:
                self.probe()
            except Exception as error:
                self._failure = error
                self._stop.set()
                try:
                    self.abort()
                except Exception as abort_error:
                    self.abort_error = f'{type(abort_error).__name__}: {abort_error}'
                self.raise_if_failed()

    def start(self):
        self.check_now()
        self._thread.start()

    def _monitor(self):
        while not self._stop.wait(self.interval):
            try:
                self.check_now()
            except OccupancyViolation:
                return

    def stop(self):
        self._stop.set()
        if self.running:
            self._thread.join(timeout=30)
            if self.running:
                raise RuntimeError('occupancy_monitor_did_not_stop')
