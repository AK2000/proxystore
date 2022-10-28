"""ProxyStore endpoint commands tests."""
from __future__ import annotations

import logging
import os
import time
import uuid
from multiprocessing import Process
from unittest import mock

import pytest

from proxystore.endpoint.commands import configure_endpoint
from proxystore.endpoint.commands import EndpointStatus
from proxystore.endpoint.commands import get_status
from proxystore.endpoint.commands import list_endpoints
from proxystore.endpoint.commands import remove_endpoint
from proxystore.endpoint.commands import start_endpoint
from proxystore.endpoint.commands import stop_endpoint
from proxystore.endpoint.config import EndpointConfig
from proxystore.endpoint.config import get_configs
from proxystore.endpoint.config import get_pid_filepath
from proxystore.endpoint.config import read_config
from proxystore.endpoint.config import write_config

_NAME = 'default'
_UUID = uuid.uuid4()
_PORT = 1234
_SERVER = None


def test_get_status(tmp_dir, caplog) -> None:
    endpoint_dir = os.path.join(tmp_dir, _NAME)
    assert not os.path.isdir(endpoint_dir)

    # Returns UNKNOWN if directory does not exist
    assert get_status(_NAME, tmp_dir) == EndpointStatus.UNKNOWN
    with mock.patch(
        'proxystore.endpoint.commands.home_dir',
        return_value=tmp_dir,
    ):
        assert get_status(_NAME) == EndpointStatus.UNKNOWN

    os.makedirs(endpoint_dir, exist_ok=True)

    # Returns UNKNOWN if config is not readable
    assert get_status(_NAME, tmp_dir) == EndpointStatus.UNKNOWN

    with mock.patch(
        'proxystore.endpoint.commands.read_config',
        return_value=None,
    ):
        # Returns STOPPED if PID file does not exist
        assert get_status(_NAME, tmp_dir) == EndpointStatus.STOPPED

        with open(get_pid_filepath(endpoint_dir), 'w') as f:
            f.write('0')

        with mock.patch('psutil.pid_exists') as mock_exists:
            # Return RUNNING if PID exists
            mock_exists.return_value = True
            assert get_status(_NAME, tmp_dir) == EndpointStatus.RUNNING

            # Return HANGING if PID does not exists
            mock_exists.return_value = False
            assert get_status(_NAME, tmp_dir) == EndpointStatus.HANGING


def test_configure_endpoint_basic(tmp_dir, caplog) -> None:
    caplog.set_level(logging.INFO)

    rv = configure_endpoint(
        name=_NAME,
        port=_PORT,
        server=_SERVER,
        proxystore_dir=tmp_dir,
    )
    assert rv == 0

    endpoint_dir = os.path.join(tmp_dir, _NAME)
    assert os.path.exists(endpoint_dir)

    cfg = read_config(endpoint_dir)
    assert cfg.name == _NAME
    assert cfg.host is None
    assert cfg.port == _PORT
    assert cfg.server == _SERVER

    assert any(
        [
            str(cfg.uuid) in record.message and record.levelname == 'INFO'
            for record in caplog.records
        ],
    )


def test_configure_endpoint_home_dir(tmp_dir) -> None:
    with mock.patch(
        'proxystore.endpoint.commands.home_dir',
        return_value=tmp_dir,
    ):
        rv = configure_endpoint(
            name=_NAME,
            port=_PORT,
            server=_SERVER,
        )
    assert rv == 0

    endpoint_dir = os.path.join(tmp_dir, _NAME)
    assert os.path.exists(endpoint_dir)


def test_configure_endpoint_invalid_name(caplog) -> None:
    caplog.set_level(logging.ERROR)

    rv = configure_endpoint(
        name='abc?',
        port=_PORT,
        server=_SERVER,
    )
    assert rv == 1

    assert any(['alphanumeric' in record.message for record in caplog.records])


def test_configure_endpoint_already_exists_error(tmp_dir, caplog) -> None:
    caplog.set_level(logging.ERROR)

    rv = configure_endpoint(
        name=_NAME,
        port=_PORT,
        server=_SERVER,
        proxystore_dir=tmp_dir,
    )
    assert rv == 0

    rv = configure_endpoint(
        name=_NAME,
        port=_PORT,
        server=_SERVER,
        proxystore_dir=tmp_dir,
    )
    assert rv == 1

    assert any(
        ['already exists' in record.message for record in caplog.records],
    )


def test_list_endpoints(tmp_dir, caplog) -> None:
    caplog.set_level(logging.INFO)

    names = ['ep1', 'ep2', 'ep3']
    # Raise logging level while creating endpoint so we just get logs from
    # list_endpoints()
    with caplog.at_level(logging.CRITICAL):
        for name in names:
            configure_endpoint(
                name=name,
                port=_PORT,
                server=_SERVER,
                proxystore_dir=tmp_dir,
            )

    rv = list_endpoints(proxystore_dir=tmp_dir)
    assert rv == 0

    assert len(caplog.records) == len(names) + 2
    for name in names:
        assert any([name in record.message for record in caplog.records])


def test_list_endpoints_empty(tmp_dir, caplog) -> None:
    caplog.set_level(logging.INFO)

    with mock.patch(
        'proxystore.endpoint.commands.home_dir',
        return_value=tmp_dir,
    ):
        rv = list_endpoints()
    assert rv == 0

    assert len(caplog.records) == 1
    assert 'No valid endpoint configurations' in caplog.records[0].message


def test_remove_endpoint(tmp_dir, caplog) -> None:
    caplog.set_level(logging.INFO)

    configure_endpoint(
        name=_NAME,
        port=_PORT,
        server=_SERVER,
        proxystore_dir=tmp_dir,
    )
    assert len(get_configs(tmp_dir)) == 1

    remove_endpoint(_NAME, proxystore_dir=tmp_dir)
    assert len(get_configs(tmp_dir)) == 0

    assert any(
        ['Removed endpoint' in record.message for record in caplog.records],
    )


def test_remove_endpoints_does_not_exist(tmp_dir, caplog) -> None:
    caplog.set_level(logging.ERROR)

    with mock.patch(
        'proxystore.endpoint.commands.home_dir',
        return_value=tmp_dir,
    ):
        rv = remove_endpoint(_NAME)
    assert rv == 1

    assert any(
        ['does not exist' in record.message for record in caplog.records],
    )


@pytest.mark.parametrize(
    'status',
    (EndpointStatus.RUNNING, EndpointStatus.HANGING),
)
def test_remove_endpoint_running(status, tmp_dir, caplog) -> None:
    os.makedirs(os.path.join(tmp_dir, _NAME), exist_ok=True)

    with mock.patch(
        'proxystore.endpoint.commands.home_dir',
        return_value=tmp_dir,
    ), mock.patch(
        'proxystore.endpoint.commands.get_status',
        return_value=status,
    ):
        rv = remove_endpoint(_NAME)
    assert rv == 1

    assert any(
        ['must be stopped' in record.message for record in caplog.records],
    )


def test_start_endpoint(tmp_dir) -> None:
    configure_endpoint(
        name=_NAME,
        port=_PORT,
        server=_SERVER,
        proxystore_dir=tmp_dir,
    )
    with mock.patch('proxystore.endpoint.commands.serve', autospec=True):
        rv = start_endpoint(_NAME, proxystore_dir=tmp_dir)
    assert rv == 0


def test_start_endpoint_detached(tmp_dir, caplog) -> None:
    caplog.set_level(logging.INFO)

    configure_endpoint(
        name=_NAME,
        port=_PORT,
        server=_SERVER,
        proxystore_dir=tmp_dir,
    )
    with mock.patch(
        'proxystore.endpoint.commands.serve',
        autospec=True,
    ), mock.patch('daemon.DaemonContext', autospec=True):
        rv = start_endpoint(_NAME, detach=True, proxystore_dir=tmp_dir)
    assert rv == 0

    assert any(['daemon' in record.message for record in caplog.records])


def test_start_endpoint_running(tmp_dir, caplog) -> None:
    caplog.set_level(logging.ERROR)

    with mock.patch(
        'proxystore.endpoint.commands.home_dir',
        return_value=tmp_dir,
    ), mock.patch(
        'proxystore.endpoint.commands.get_status',
        return_value=EndpointStatus.RUNNING,
    ):
        rv = start_endpoint(_NAME)
    assert rv == 1

    assert any(
        ['already running' in record.message for record in caplog.records],
    )


def test_start_endpoint_does_not_exist(tmp_dir, caplog) -> None:
    caplog.set_level(logging.ERROR)

    with mock.patch(
        'proxystore.endpoint.commands.home_dir',
        return_value=tmp_dir,
    ):
        rv = start_endpoint(_NAME)
    assert rv == 1

    assert any(
        ['does not exist' in record.message for record in caplog.records],
    )


def test_start_endpoint_missing_config(tmp_dir, caplog) -> None:
    caplog.set_level(logging.ERROR)

    os.makedirs(os.path.join(tmp_dir, _NAME))
    rv = start_endpoint(_NAME, proxystore_dir=tmp_dir)
    assert rv == 1

    assert any(
        [
            'does not contain a valid configuration' in record.message
            for record in caplog.records
        ],
    )


def test_start_endpoint_bad_config(tmp_dir, caplog) -> None:
    caplog.set_level(logging.ERROR)

    endpoint_dir = os.path.join(tmp_dir, _NAME)
    os.makedirs(endpoint_dir)
    with open(os.path.join(endpoint_dir, 'endpoint.json'), 'w') as f:
        f.write('not valid json')

    rv = start_endpoint(_NAME, proxystore_dir=tmp_dir)
    assert rv == 1

    assert any(
        ['Unable to parse' in record.message for record in caplog.records],
    )


def test_start_endpoint_hanging_different_host(tmp_dir, caplog) -> None:
    caplog.set_level(logging.ERROR)

    endpoint_dir = os.path.join(tmp_dir, _NAME)

    config = EndpointConfig(name=_NAME, uuid=_UUID, host='abcd', port=1234)
    write_config(config, endpoint_dir)

    pid_file = get_pid_filepath(endpoint_dir)
    with open(pid_file, 'w') as f:
        f.write('1')

    with mock.patch('psutil.pid_exists', return_value=False):
        rv = start_endpoint(_NAME, proxystore_dir=tmp_dir)
    assert rv == 1

    assert any(
        [
            'on a host named abcd' in record.message
            for record in caplog.records
        ],
    )


def test_start_endpoint_old_pid_file(tmp_dir, caplog) -> None:
    caplog.set_level(logging.DEBUG)

    endpoint_dir = os.path.join(tmp_dir, _NAME)

    config = EndpointConfig(name=_NAME, uuid=_UUID, host=None, port=1234)
    write_config(config, endpoint_dir)

    pid_file = get_pid_filepath(endpoint_dir)
    with open(pid_file, 'w') as f:
        f.write('1')

    with mock.patch('psutil.pid_exists', return_value=False), mock.patch(
        'proxystore.endpoint.commands.serve',
        autospec=True,
    ):
        rv = start_endpoint(_NAME, proxystore_dir=tmp_dir)
    assert rv == 0

    assert any(
        [
            'Removing invalid PID file' in record.message
            for record in caplog.records
            if record.levelno == logging.DEBUG
        ],
    )


@pytest.mark.timeout(2)
def test_stop_endpoint(tmp_dir) -> None:
    endpoint_dir = os.path.join(tmp_dir, _NAME)
    configure_endpoint(
        name=_NAME,
        port=_PORT,
        server=_SERVER,
        proxystore_dir=tmp_dir,
    )

    # Create a fake process to kill
    p = Process(target=time.sleep, args=(1000,))
    p.start()

    pid_file = get_pid_filepath(endpoint_dir)
    with open(pid_file, 'w') as f:
        f.write(str(p.pid))

    with mock.patch(
        'proxystore.endpoint.commands.home_dir',
        return_value=tmp_dir,
    ):
        rv = stop_endpoint(_NAME)
    assert rv == 0
    assert not os.path.exists(pid_file)

    # Process was terminated so this should happen immediately
    p.join()


def test_stop_endpoint_unknown(tmp_dir, caplog) -> None:
    caplog.set_level(logging.INFO)
    with mock.patch(
        'proxystore.endpoint.commands.get_status',
        return_value=EndpointStatus.UNKNOWN,
    ):
        rv = stop_endpoint(_NAME, proxystore_dir=tmp_dir)
    assert rv == 1

    assert any(
        ['does not exist' in record.message for record in caplog.records],
    )


def test_stop_endpoint_not_running(tmp_dir, caplog) -> None:
    caplog.set_level(logging.INFO)
    with mock.patch(
        'proxystore.endpoint.commands.get_status',
        return_value=EndpointStatus.STOPPED,
    ):
        rv = stop_endpoint(_NAME, proxystore_dir=tmp_dir)
    assert rv == 0

    assert any(['not running' in record.message for record in caplog.records])


def test_stop_endpoint_hanging_different_host(tmp_dir, caplog) -> None:
    caplog.set_level(logging.ERROR)
    endpoint_dir = os.path.join(tmp_dir, _NAME)

    config = EndpointConfig(name=_NAME, uuid=_UUID, host='abcd', port=1234)
    write_config(config, endpoint_dir)

    pid_file = get_pid_filepath(endpoint_dir)
    with open(pid_file, 'w') as f:
        f.write('1')

    with mock.patch('psutil.pid_exists', return_value=False):
        rv = stop_endpoint(_NAME, proxystore_dir=tmp_dir)
    assert rv == 1

    assert any(
        [
            'on a host named abcd' in record.message
            for record in caplog.records
        ],
    )


def test_stop_endpoint_dangling_pid_file(tmp_dir, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    endpoint_dir = os.path.join(tmp_dir, _NAME)

    config = EndpointConfig(name=_NAME, uuid=_UUID, host=None, port=1234)
    write_config(config, endpoint_dir)

    pid_file = get_pid_filepath(endpoint_dir)
    with open(pid_file, 'w') as f:
        f.write('1')

    with mock.patch('psutil.pid_exists', return_value=False):
        rv = stop_endpoint(_NAME, proxystore_dir=tmp_dir)
    assert rv == 0

    assert not os.path.exists(pid_file)

    assert any(
        [
            'Removing invalid PID file' in record.message
            for record in caplog.records
            if record.levelno == logging.DEBUG
        ],
    )
    assert any(
        [
            'not running' in record.message
            for record in caplog.records
            if record.levelno == logging.INFO
        ],
    )
