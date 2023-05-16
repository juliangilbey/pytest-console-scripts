from __future__ import annotations

import io
import logging
import os
import shutil
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Any, Callable
from unittest import mock

import pytest

if sys.version_info < (3, 10):
    import importlib_metadata
else:
    import importlib.metadata as importlib_metadata

StreamMock = io.StringIO


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup('console-scripts')
    group.addoption(
        '--script-launch-mode',
        metavar='inprocess|subprocess|both',
        action='store',
        dest='script_launch_mode',
        default=None,
        help='how to run python scripts under test (default: inprocess)'
    )
    group.addoption(
        '--hide-run-results',
        action='store_true',
        dest='hide_run_results',
        default=False,
        help="don't print out script run results on failures or when "
             "output capturing is disabled"
    )
    parser.addini(
        'script_launch_mode',
        'how to run python scripts under test (inprocess|subprocess|both)'
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        'markers',
        'script_launch_mode: how to run python scripts under test '
        '(inprocess|subprocess|both)',
    )


def _get_mark_mode(metafunc: pytest.Metafunc) -> str | None:
    """Return launch mode as indicated by test function marker or None."""
    marker = metafunc.definition.get_closest_marker('script_launch_mode')
    if marker:
        return str(marker.args[0])
    return None


def _is_nonexecutable_python_file(command: str | os.PathLike[str]) -> bool:
    """Check if `command` is a Python file with no executable mode set."""
    command = Path(command)
    mode = command.stat().st_mode
    if mode & os.X_OK:
        return False
    return command.suffix == '.py'


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Parametrize script_launch_mode fixture.

    Checks the configuration sources in this order:
    - `script_launch_mode` mark on the test,
    - `--script-launch-mode` option,
    - `script_launch_mode` configuration option in [pytest] section of the
      pyest config file.

    This process yields a value that can be one of:
    - "inprocess" -- The script will be run via loading its main function
      into the test runner process and mocking the environment.
    - "subprocess" -- The script will be run via `subprocess` module.
    - "both" -- The test will be run twice: once in inprocess mode and once
      in subprocess mode.
    - None -- Same as "inprocess".
    """
    if 'script_launch_mode' not in metafunc.fixturenames:
        return

    mark_mode = _get_mark_mode(metafunc)
    option_mode = metafunc.config.option.script_launch_mode
    config_mode = metafunc.config.getini('script_launch_mode')

    mode = mark_mode or option_mode or config_mode or 'inprocess'

    if mode in {'inprocess', 'subprocess'}:
        metafunc.parametrize('script_launch_mode', [mode], indirect=True)
    elif mode == 'both':
        metafunc.parametrize('script_launch_mode', ['inprocess', 'subprocess'],
                             indirect=True)
    else:
        raise ValueError(f'Invalid script launch mode: {mode}')


class RunResult:
    """Result of running a script."""

    def __init__(
        self, returncode: int, stdout: str, stderr: str, print_result: bool
    ) -> None:
        self.success = returncode == 0
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        if print_result:
            self.print()

    def print(self) -> None:
        print('# Script return code:', self.returncode)
        print('# Script stdout:', self.stdout, sep='\n')
        print('# Script stderr:', self.stderr, sep='\n')


class ScriptRunner:
    """Fixture for running python scripts under test."""

    def __init__(
        self, launch_mode: str,
        rootdir: str | os.PathLike[str],
        print_result: bool = True
    ) -> None:
        assert launch_mode in {'inprocess', 'subprocess'}
        self.launch_mode = launch_mode
        self.print_result = print_result
        self.rootdir = rootdir

    def __repr__(self) -> str:
        return f'<ScriptRunner {self.launch_mode}>'

    def run(self, command: str, *arguments: str, **options: Any) -> RunResult:
        options.setdefault('print_result', self.print_result)
        if options['print_result']:
            print('# Running console script:', command, *arguments)

        if self.launch_mode == 'inprocess':
            return self.run_inprocess(command, *arguments, **options)
        return self.run_subprocess(command, *arguments, **options)

    def _save_and_reset_logger(self) -> dict[str, Any]:
        """Do a very basic reset of the root logger and return its config.

        This allows scripts to call logging.basicConfig(...) and have
        it work as expected. It might not work for more sophisticated logging
        setups but it's simple and covers the basic usage whereas implementing
        a comprehensive fix is impossible in a compatible way.

        """
        logger = logging.getLogger()
        config = {
            'level': logger.level,
            'handlers': logger.handlers,
            'disabled': logger.disabled,
        }
        logger.handlers = []
        logger.disabled = False
        logger.setLevel(logging.NOTSET)
        return config

    def _restore_logger(self, config: dict[str, Any]) -> None:
        """Restore logger to previous configuration."""
        logger = logging.getLogger()
        logger.handlers = config['handlers']
        logger.disabled = config['disabled']
        logger.setLevel(config['level'])

    def _locate_script(self, command: str, **options: Any) -> str:
        """Locate script in PATH or in current directory."""
        script_path = shutil.which(
            command,
            path=options.get('env', {}).get('PATH', None),
        )
        if script_path is not None:
            return script_path

        cwd = options.get('cwd', os.getcwd())
        script_path = os.path.join(cwd, command)
        if os.path.exists(script_path):
            return script_path

        raise FileNotFoundError('Cannot find ' + command)

    def _load_script(
        self, command: str, **options: Any
    ) -> Callable[[], int | None]:
        """Load target script via entry points or compile/exec."""
        entry_points = tuple(
            importlib_metadata.entry_points(
                group='console_scripts', name=command
            )
        )
        if entry_points:
            def console_script() -> int | None:
                s: Callable[[], int | None] = entry_points[0].load()
                return s()
            return console_script

        script_path = self._locate_script(command, **options)

        def exec_script() -> int:
            with open(script_path, 'rt', encoding='utf-8') as script:
                compiled = compile(script.read(), str(script), 'exec', flags=0)
                exec(compiled, {'__name__': '__main__'})
            return 0

        return exec_script

    def run_inprocess(
        self, command: str, *arguments: str, **options: Any
    ) -> RunResult:
        cmdargs = [command] + list(arguments)
        script = self._load_script(command, **options)
        stdin = options.get('stdin', StreamMock())
        stdout = StreamMock()
        stderr = StreamMock()
        stdin_patch = mock.patch('sys.stdin', new=stdin)
        stdout_patch = mock.patch('sys.stdout', new=stdout)
        stderr_patch = mock.patch('sys.stderr', new=stderr)
        argv_patch = mock.patch('sys.argv', new=cmdargs)
        saved_dir = os.getcwd()
        logger_conf = self._save_and_reset_logger()

        if 'env' in options:
            old_env = os.environ
            os.environ = options['env']

        if 'cwd' in options:
            os.chdir(options['cwd'])

        print_result = options.pop('print_result')

        with stdin_patch, stdout_patch, stderr_patch, argv_patch:
            try:
                returncode = script()
                if returncode is None:
                    returncode = 0  # None also means success.
            except SystemExit as exc:
                if isinstance(exc.code, str):
                    stderr.write(f'{exc}\n')
                    returncode = 1
                elif exc.code is None:
                    returncode = 0
                else:
                    returncode = exc.code
            except Exception:
                returncode = 1
                try:
                    et, ev, tb = sys.exc_info()
                    assert tb
                    # Hide current frame from the stack trace.
                    traceback.print_exception(et, ev, tb.tb_next)
                finally:
                    del tb

        self._restore_logger(logger_conf)
        os.chdir(saved_dir)

        if 'env' in options:
            os.environ = old_env

        return RunResult(returncode, stdout.getvalue(), stderr.getvalue(),
                         print_result)

    def run_subprocess(
        self, command: str, *arguments: str, **options: Any
    ) -> RunResult:
        stdin_input = None
        if 'stdin' in options:
            stdin_input = options.pop('stdin').read()

        options.setdefault('universal_newlines', True)
        print_result = options.pop('print_result')

        cmd_args = [command] + list(arguments)
        script_path = self._locate_script(command, **options)
        if _is_nonexecutable_python_file(script_path):
            cmd_args = [sys.executable or 'python'] + cmd_args

        cp = subprocess.run(
            cmd_args,
            input=stdin_input,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **options,
        )
        return RunResult(cp.returncode, cp.stdout, cp.stderr, print_result)


@pytest.fixture
def script_launch_mode(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def script_cwd(tmp_path: Path) -> Path:
    work_dir = tmp_path / 'script-cwd'
    work_dir.mkdir()
    return work_dir


@pytest.fixture
def script_runner(
    request: pytest.FixtureRequest, script_cwd: Path, script_launch_mode: str
) -> ScriptRunner:
    print_result = not request.config.getoption("--hide-run-results")
    return ScriptRunner(script_launch_mode, script_cwd, print_result)