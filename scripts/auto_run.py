#!/usr/bin/env python3
"""Poll origin/main and supervise main_fastapi.py (Linux, standard library only)."""
import argparse
import fcntl
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
LOG = logging.getLogger('auto_run')
STOP = threading.Event()


def git(*args):
    env = {**os.environ, 'GIT_TERMINAL_PROMPT': '0'}
    result = subprocess.run(
        ['git', *args], cwd=ROOT, env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()


def update():
    if git('branch', '--show-current') != 'main':
        raise RuntimeError('main 브랜치에서 실행해야 합니다.')
    if git('status', '--porcelain', '--untracked-files=no'):
        raise RuntimeError('추적 파일에 로컬 변경이 있어 자동 업데이트를 건너뜁니다.')
    git('fetch', 'origin', 'main')
    before = git('rev-parse', 'HEAD')
    remote = git('rev-parse', 'refs/remotes/origin/main')
    if before == remote:
        return before
    # 로컬 커밋/분기 이력이 있으면 덮어쓰지 않는다.
    if git('rev-list', '--count', 'refs/remotes/origin/main..HEAD') != '0':
        raise RuntimeError('로컬 커밋이 있어 자동 업데이트를 건너뜁니다.')
    git('pull', '--ff-only', 'origin', 'main')
    revision = git('rev-parse', 'HEAD')
    LOG.info('업데이트 완료: %s -> %s', before[:7], revision[:7])
    return revision


def validate():
    for name in git('ls-files', '*.py').splitlines():
        path = ROOT / name
        compile(path.read_bytes(), str(path), 'exec')


def stop_child(child):
    if child is None or child.poll() is not None:
        return
    os.killpg(child.pid, signal.SIGTERM)
    try:
        child.wait(timeout=30)
    except subprocess.TimeoutExpired:
        LOG.warning('종료 대기 초과: 앱을 강제 종료합니다.')
        os.killpg(child.pid, signal.SIGKILL)
        child.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--interval', type=int, default=30, help='GitHub 확인 간격(초)')
    args = parser.parse_args()
    if args.interval < 5:
        parser.error('--interval must be at least 5 seconds')
    os.chdir(ROOT)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    # 동일 저장소에서 감독 프로세스가 중복 실행되지 않도록 잠근다.
    with (ROOT / '.auto_run.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            LOG.error('이미 자동 실행 프로세스가 동작 중입니다.')
            return 1
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: STOP.set())
        child = None
        running_revision = None
        try:
            with (ROOT / 'output.log').open('ab', buffering=0) as output:
                while not STOP.is_set():
                    try:
                        update()
                    except (RuntimeError, subprocess.TimeoutExpired) as error:
                        LOG.warning('GitHub 동기화 실패; 현재 앱은 유지합니다: %s', error)
                    if STOP.is_set():
                        break
                    try:
                        revision = git('rev-parse', 'HEAD')
                        if child is None or child.poll() is not None or revision != running_revision:
                            validate()
                            stop_child(child)
                            if STOP.is_set():
                                break
                            child = subprocess.Popen(
                                [sys.executable, '-u', 'main_fastapi.py'], cwd=ROOT,
                                stdout=output, stderr=subprocess.STDOUT, start_new_session=True,
                            )
                            running_revision = revision
                            LOG.info('앱 실행: PID=%s, commit=%s', child.pid, revision[:7])
                    except (OSError, RuntimeError, SyntaxError, subprocess.TimeoutExpired) as error:
                        LOG.exception('앱 실행/검증 실패: %s', error)
                    STOP.wait(args.interval)
        finally:
            stop_child(child)
    return 0


if __name__ == '__main__':
    sys.exit(main())
