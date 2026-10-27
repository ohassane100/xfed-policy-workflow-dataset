"""Run private Ollama workers using the server installation and model store."""

import csv
import json
import os
from pathlib import Path, PurePosixPath
import shlex
import socket
import subprocess
from tempfile import NamedTemporaryFile
from threading import Event, Lock
from time import monotonic
from urllib.error import URLError
from urllib.request import urlopen

from . import OllamaPool


def nvidia_gpus():
    """Map physical nvidia-smi indices to stable CUDA UUIDs."""
    try:
        result = subprocess.run(['nvidia-smi', '--query-gpu=index,uuid', '--format=csv,noheader,nounits'],
                                capture_output=True, text=True, timeout=10, check=True)
        devices = [dict(index=int(row[0].strip()), uuid=row[1].strip())
                   for row in csv.reader(result.stdout.splitlines()) if row]
        if (not devices or any(device['index'] < 0 or not device['uuid'].startswith('GPU-') for device in devices) or
                len({device['index'] for device in devices}) != len(devices) or
                len({device['uuid'] for device in devices}) != len(devices)):
            raise ValueError('No unique NVIDIA devices were reported.')
        return sorted(devices, key=lambda device: device['index'])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError) as error:
        raise RuntimeError(f'Cannot discover NVIDIA GPU indices and UUIDs with nvidia-smi: {error}') from error


def worker_pid(log_path):
    with log_path.open('rb') as stream:
        lines = stream.read(8192).decode('utf-8', errors='replace').splitlines()
    return next((int(line.split('=', 1)[1]) for line in lines
                 if line.startswith('XFED_OLLAMA_PID=') and line.split('=', 1)[1].isdigit()
                 and int(line.split('=', 1)[1]) > 1), None)


class OllamaServicePool(OllamaPool):
    def __init__(self, base_urls, model, worker_count=1, worker_gpus=None, service=None, **options):
        if type(worker_count) is not int or not 1 <= worker_count <= 32:
            raise ValueError('Choose between 1 and 32 workers for the selected model.')
        gpus = [None] * worker_count if worker_gpus is None else worker_gpus
        if (not isinstance(gpus, list) or len(gpus) != worker_count or
                any(gpu is not None and (type(gpu) is not int or gpu < 0) for gpu in gpus)):
            raise ValueError('Choose one nonnegative GPU index or Auto for each worker.')
        pinned = any(gpu is not None for gpu in gpus)
        if pinned and (None in gpus or len(set(gpus)) != worker_count):
            raise ValueError('Choose a different GPU for every worker, or use Auto for all workers.')
        if options.get('main_gpu') is not None:
            raise ValueError('Use execution.worker_gpus instead of main_gpu for managed workers.')
        defaults = dict(binary='/local/ollama/bin/ollama', user='ollama', models_dir=None,
                        startup_timeout=30)
        if service is not None:
            if not isinstance(service, dict) or set(service) - set(defaults):
                raise ValueError('service supports binary, user, models_dir, and startup_timeout.')
            defaults.update(service)
        if (not isinstance(defaults['binary'], str) or not PurePosixPath(defaults['binary']).is_absolute() or
                not isinstance(defaults['user'], str) or not defaults['user'].strip() or
                (defaults['models_dir'] is not None and
                 (not isinstance(defaults['models_dir'], str) or not PurePosixPath(defaults['models_dir']).is_absolute())) or
                type(defaults['startup_timeout']) not in (int, float) or defaults['startup_timeout'] <= 0):
            raise ValueError('Set an absolute Ollama binary/model-store path, user, and positive startup_timeout.')
        # Validate generation settings before allocating ports or starting any processes.
        self._source_client = OllamaPool(base_urls, model, **options).clients[0]
        self.service = defaults
        self.source_urls = list(base_urls)
        self.cancel = options.get('cancel') or Event()
        options['cancel'] = self.cancel
        self._service_lock, self._closed, self._started = Lock(), False, False
        self._servers = []
        reservations = []
        try:
            for _ in range(worker_count if pinned else 1):
                reservation = socket.socket()
                reservation.bind(('127.0.0.1', 0))
                reservations.append(reservation)
            urls = [f'http://127.0.0.1:{s.getsockname()[1]}' for s in reservations]
            super().__init__(urls, model, workers_per_endpoint=1 if pinned else worker_count, **options)
        finally:
            for reservation in reservations:
                reservation.close()
        for client, gpu in zip(self.clients, gpus):
            # The server sees its chosen GPU as device 0. Never send the physical
            # GPU index as an inference option to that isolated server.
            client.gpu = gpu
            client.placement_status = 'pending' if gpu is not None else 'auto'
        self.worker_gpus = list(gpus)

    def _environment(self):
        import pwd
        environment = {}
        try:
            result = subprocess.run(['systemctl', 'show', 'ollama.service', '--property=Environment', '--value'],
                                    capture_output=True, text=True, timeout=10, check=False)
            if result.returncode == 0:
                environment = dict(item.split('=', 1) for item in shlex.split(result.stdout) if '=' in item)
        except (OSError, subprocess.TimeoutExpired):
            pass  # An explicit models_dir also supports hosts without systemd.
        models_dir = self.service['models_dir'] or environment.get('OLLAMA_MODELS')
        if not models_dir:
            models_dir = str(Path(pwd.getpwnam(self.service['user']).pw_dir) / '.ollama/models')
        # Preserve server GPU visibility and backend configuration in Auto mode.
        settings = {key: value for key, value in environment.items()
                    if key.startswith('OLLAMA_') or key in ('CUDA_VISIBLE_DEVICES', 'LD_LIBRARY_PATH')}
        settings.update(OLLAMA_MODELS=models_dir, OLLAMA_MAX_LOADED_MODELS='1')
        return settings

    def _start(self):
        if os.name != 'posix':
            raise RuntimeError('Managed Ollama workers must run on the Linux GPU server, such as Gorina5.')
        environment = self._environment()
        self.gpu_devices = nvidia_gpus() if any(gpu is not None for gpu in self.worker_gpus) else []
        devices = {device['index']: device['uuid'] for device in self.gpu_devices}
        if any(gpu is not None and gpu not in devices for gpu in self.worker_gpus):
            raise ValueError(f'Choose detected NVIDIA GPU indices {sorted(devices)}. '
                             f'At most {min(32, len(devices))} workers can use different GPUs on this host.')
        for index, client in enumerate(self.clients):
            if client.gpu is not None:
                client.gpu_uuid = devices[client.gpu]
                client.verify_placement = lambda client=client, index=index: self._verify_gpu(client, index)
        urls = list(dict.fromkeys(c.url.removesuffix('/api/chat') for c in self.clients))
        for index, url in enumerate(urls):
            if self.cancel.is_set():
                raise InterruptedError('Worker startup cancelled.')
            settings = dict(environment, OLLAMA_HOST=url,
                            OLLAMA_NUM_PARALLEL=str(self.workers_per_endpoint))
            if self.worker_gpus[index] is not None:
                settings.update(CUDA_VISIBLE_DEVICES=devices[self.worker_gpus[index]],
                                CUDA_DEVICE_ORDER='PCI_BUS_ID', OLLAMA_VULKAN='0',
                                GGML_VK_VISIBLE_DEVICES='-1', ROCR_VISIBLE_DEVICES='-1',
                                HIP_VISIBLE_DEVICES='-1')
                if not settings.get('OLLAMA_LLM_LIBRARY', '').startswith('cuda'):
                    settings.pop('OLLAMA_LLM_LIBRARY', None)
            # A separate session gives every worker a known process group. Record
            # its PID before exec so cleanup can signal it as the ollama user,
            # even when sudo changes identity or forks a supervising process.
            command = ['sudo', '-n', '-H', '-u', self.service['user'], '--',
                       'setsid', '--wait', 'sh', '-c',
                       'printf "XFED_OLLAMA_PID=%s\\n" "$$"; exec "$@"', 'xfed-ollama', 'env',
                       *[f'{key}={value}' for key, value in settings.items()], self.service['binary'], 'serve']
            with self._service_lock:
                if self._closed or self.cancel.is_set():
                    raise InterruptedError('Worker startup cancelled.')
                log = NamedTemporaryFile(prefix='xfed-ollama-', suffix='.log', delete=False)
                log_path = Path(log.name)
                try:
                    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log,
                                               stderr=subprocess.STDOUT, start_new_session=True)
                except BaseException:
                    log.close()
                    log_path.unlink(missing_ok=True)
                    raise
                log.close()
                self._servers.append((process, log_path))
            deadline = monotonic() + self.service['startup_timeout']
            while True:
                if self.cancel.is_set():
                    raise InterruptedError('Worker startup cancelled.')
                if process.poll() is not None:
                    detail = log_path.read_bytes()[-3000:].decode('utf-8', errors='replace')
                    raise RuntimeError('Could not start an Ollama worker as ' + self.service['user'] + ': ' + detail)
                try:
                    with urlopen(url + '/api/version', timeout=1) as response:
                        response.read()
                    break
                except (URLError, OSError):
                    if monotonic() >= deadline:
                        raise RuntimeError(f'Ollama worker at {url} did not start within the startup timeout.')
                    self.cancel.wait(0.1)
        self._started = True

    def _verify_gpu(self, client, index):
        """Check this server's actual runner processes before accepting a response."""
        if self.cancel.is_set():
            raise InterruptedError('GPU verification cancelled.')
        client.observed_gpus = None
        try:
            with self._service_lock:
                process, log_path = self._servers[index]
                server_pid = worker_pid(log_path)
            if server_pid is None or process.poll() is not None:
                raise RuntimeError('The worker server PID is unavailable.')
            processes = subprocess.run(['ps', '-eo', 'pid=,ppid='], capture_output=True,
                                       text=True, timeout=10, check=True)
            parents = {}
            for line in processes.stdout.splitlines():
                parts = line.split()
                if len(parts) == 2:
                    parents[int(parts[0])] = int(parts[1])
            applications = subprocess.run(['nvidia-smi', '--query-compute-apps=pid,gpu_uuid',
                                          '--format=csv,noheader,nounits'], capture_output=True,
                                         text=True, timeout=10, check=True)
            def belongs_to_worker(pid):
                seen = set()
                while pid > 1 and pid not in seen:
                    if pid == server_pid:
                        return True
                    seen.add(pid)
                    pid = parents.get(pid, 0)
                return False
            observed = {row[1].strip() for row in csv.reader(applications.stdout.splitlines())
                        if len(row) == 2 and row[0].strip().isdigit() and belongs_to_worker(int(row[0]))}
            indices = {device['uuid']: device['index'] for device in self.gpu_devices}
            client.observed_gpus = [indices.get(uuid, uuid) for uuid in sorted(observed)]
            if observed != {client.gpu_uuid}:
                raise RuntimeError(f'Requested GPU {client.gpu} ({client.gpu_uuid}), '
                                   f'observed {client.observed_gpus or "no NVIDIA runner (CPU fallback or process visibility unavailable)"}.')
            with urlopen(client.url.removesuffix('/api/chat') + '/api/ps', timeout=10) as response:
                status = json.load(response)
            models = status.get('models') if isinstance(status, dict) else None
            if not isinstance(models, list):
                raise ValueError('The worker returned invalid model residency information.')
            loaded = next((model for model in models if isinstance(model, dict) and
                           model.get('name') == client.model and model.get('digest') == client.model_digest), None)
            if (not loaded or type(loaded.get('size_vram')) not in (int, float) or loaded['size_vram'] <= 0):
                raise RuntimeError('The worker does not report the selected model resident on a GPU; '
                                   'CPU fallback or model unloading cannot satisfy pinned GPU placement.')
            client.placement_status, client.placement_error = 'verified', None
        except (OSError, subprocess.SubprocessError, ValueError, IndexError, RuntimeError) as error:
            client.placement_status = 'failed'
            client.placement_error = str(error)
            raise RuntimeError(f'GPU placement verification failed for {client.model}: {error}') from error

    def preflight(self):
        try:
            if self._closed or self.cancel.is_set():
                raise InterruptedError('Worker pool is closed or cancelled.')
            if not self._started:
                self._source_client.preflight()
                self._start()
            try:
                result = super().preflight()
            except ValueError as error:
                raise ValueError(f'{error} Check execution.service.models_dir for the existing model store.') from error
            if self.model_digest != self._source_client.model_digest:
                raise ValueError('Managed workers loaded a different model digest from the shared service. Check service.models_dir.')
            return result
        except BaseException:
            self.close()
            raise

    def metadata(self):
        result = super().metadata()
        result.update(managed_service=True, worker_gpus=self.worker_gpus,
                      service_binary=self.service['binary'], service_user=self.service['user'])
        return result

    def close(self):
        with self._service_lock:
            self._closed = True
            servers, self._servers = self._servers, []
        errors = []
        for process, log_path in servers:
            try:
                if process.poll() is None:
                    deadline, group = monotonic() + 1, None
                    while group is None and process.poll() is None:
                        group = worker_pid(log_path)
                        if group is not None or monotonic() >= deadline:
                            break
                        Event().wait(0.05)
                    def signal_group(signal):
                        result = subprocess.run(['sudo', '-n', '-u', self.service['user'], '--',
                                                 'kill', '-' + signal, '--', '-' + str(group)],
                                                capture_output=True, text=True, timeout=10, check=False)
                        if result.returncode and process.poll() is None:
                            raise RuntimeError(f'Could not stop worker group {group}: {result.stderr.strip()}')
                    if group is not None and int(group) > 1:
                        signal_group('TERM')
                    else:
                        # Before the session marker, only the launcher is running.
                        process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        if group is None:
                            raise RuntimeError(f'Launcher {process.pid} did not stop; no worker PID was recorded.')
                        signal_group('KILL')
                        process.wait(timeout=5)
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
                errors.append(str(error))
                with self._service_lock:
                    self._servers.append((process, log_path))
            else:
                log_path.unlink(missing_ok=True)
        if errors:
            raise RuntimeError('Could not stop all Ollama workers: ' + '; '.join(errors))
