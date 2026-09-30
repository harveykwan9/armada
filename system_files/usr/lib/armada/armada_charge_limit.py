"""Apply, persist and restore the battery charge limit (charge_control_end_threshold)."""

import argparse
import os
from pathlib import Path
import sys
import tempfile

SYS = Path(os.environ.get("ARMADA_CHARGE_LIMIT_SYS_ROOT", "/sys"))
PROC = Path(os.environ.get("ARMADA_CHARGE_LIMIT_PROC_ROOT", "/proc"))
SAVED = Path(os.environ.get("ARMADA_CHARGE_LIMIT_PATH", "/etc/armada/charge-limit"))
ATTR = "charge_control_end_threshold"
START_ATTR = "charge_control_start_threshold"
# qcom_battmgr clamps the end threshold to 55-100.
MIN_LIMIT = 55
MAX_LIMIT = 100
STEP = 5
# Resume charging this far below the limit. qcom_battmgr keeps the old start
# threshold when the end moves, so setting 60 then 100 would otherwise leave
# charging paused until 55%.
START_GAP = 5


def read(path):
    try:
        return path.read_text().strip()
    except OSError:
        return None


def power_supplies():
    root = SYS / "class/power_supply"
    try:
        return sorted(path for path in root.iterdir() if path.is_dir())
    except OSError:
        return []


def find_battery():
    # Match by type: ROCKNIX renames the battery supply on some boards.
    for path in power_supplies():
        if read(path / "type") == "Battery" and (path / ATTR).exists():
            return path
    return None


def writable(path):
    # Check the mode bits, not os.access(): every caller runs as root, for whom
    # access(W_OK) always succeeds. The kernel creates the attribute 0444 when
    # the driver doesn't make it writeable.
    try:
        return path.is_file() and bool(path.stat().st_mode & 0o222)
    except OSError:
        return False


def supported(battery):
    return battery is not None and writable(battery / ATTR)


def in_range(value):
    return isinstance(value, int) and not isinstance(value, bool) and MIN_LIMIT <= value <= MAX_LIMIT


def validate(value):
    # Step of 5 applies to user input only; firmware may store any value in range.
    if not in_range(value) or value % STEP:
        raise ValueError("invalid charge limit")
    return value


def read_int(path):
    try:
        return int(read(path))
    except (TypeError, ValueError):
        return None


def read_limit(battery):
    return read_int(battery / ATTR)


def start_path(battery):
    path = battery / START_ATTR
    return path if writable(path) else None


def thresholds(battery):
    # (end, start); start is None when the driver has no writable start threshold.
    path = start_path(battery)
    return read_limit(battery), read_int(path) if path else None


def target_thresholds(battery, value):
    return value, value - START_GAP if start_path(battery) else None


def current_limit():
    battery = find_battery()
    return read_limit(battery) if supported(battery) else None


def write(path, value, what):
    try:
        path.write_text(f"{value}\n")
    except OSError as error:
        raise RuntimeError(f"could not update the {what}: {error}") from error


def apply(value, battery):
    if not in_range(value):
        raise ValueError("invalid charge limit")
    if not supported(battery):
        raise RuntimeError("battery charge limit is not supported on this device")
    write(battery / ATTR, value, "battery charge limit")

    # Firmware may clamp the value, and qcom_battmgr reports success even when the
    # firmware rejects the request; report what it actually stored.
    applied = read_limit(battery)
    if applied is None:
        raise RuntimeError("could not read the battery charge limit")
    path = start_path(battery)
    # A start threshold below an out-of-range end would re-enable a clamped limit.
    if path and in_range(applied):
        write(path, applied - START_GAP, "charge start threshold")
    return applied


def atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def saved_limit():
    try:
        value = int(read(SAVED))
    except (TypeError, ValueError):
        return None
    return value if in_range(value) else None


def set_limit(value):
    applied = apply(value, find_battery())
    # Save what the firmware stored so restore() settles instead of rewriting on every call.
    atomic_write(SAVED, f"{applied}\n")
    return applied


def restore():
    # Returns (ready, previous). ready stays False while the thresholds can't be
    # read: qcom_battmgr registers the battery when the driver loads but answers
    # EAGAIN until the ADSP is up. previous is the (end, start) pair that was
    # rewritten, or None when nothing had to change. Raises RuntimeError if a
    # write fails.
    value = saved_limit()
    if value is None:
        return True, None
    battery = find_battery()
    if not supported(battery):
        return False, None
    current = thresholds(battery)
    if current[0] is None:
        return False, None
    if current == target_thresholds(battery, value):
        return True, None
    apply(value, battery)
    return True, current


def show(value):
    return "none" if value is None else value


def probe_restore(battery, end, start, emit):
    # Out-of-range originals (qcom_battmgr reads 0 before any limit is set) would
    # be clamped to 55 and leave the battery capped there, so use 100 instead.
    if not in_range(end):
        emit(f"note=original end {end} is outside {MIN_LIMIT}-{MAX_LIMIT}; restoring {MAX_LIMIT} instead")
        end, start = MAX_LIMIT, None
    path = start_path(battery)
    if path and not (start is not None and 0 < start < end):
        start = end - START_GAP
    try:
        # End first, so the driver keeps the start threshold below it.
        (battery / ATTR).write_text(f"{end}\n")
        if path:
            path.write_text(f"{start}\n")
    except OSError as error:
        emit(f"restore_error={error}")
        return 1
    restored = thresholds(battery)
    emit(f"restored_end={show(restored[0])} restored_start={show(restored[1])}")
    if restored != (end, start if path else None):
        emit(f"restore_error=expected end={end} start={show(start if path else None)}")
        return 1
    return 0


def probe(value=None, out=None):
    out = out or sys.stdout

    def emit(line=""):
        print(line, file=out)

    emit("# armada-charge-limit-probe")
    emit(f"kernel={os.uname().release}")
    model = (read(PROC / "device-tree/model") or "unavailable").rstrip("\0")
    emit(f"model={model}")

    emit("\n## power_supply")
    supplies = power_supplies()
    for supply in supplies:
        emit(f"{supply.name} type={read(supply / 'type') or 'unavailable'}")
    if not supplies:
        emit("power_supplies=unavailable")

    emit("\n## charge_control")
    for supply in supplies:
        if read(supply / "type") != "Battery":
            continue
        files = sorted(supply.glob("charge_control*"))
        for path in files:
            try:
                mode = oct(path.stat().st_mode & 0o777)
            except OSError:
                mode = "?"
            can_write = "yes" if writable(path) else "no"
            emit(f"{supply.name}/{path.name}={read(path) or 'unavailable'} mode={mode} writable={can_write}")
        if not files:
            emit(f"{supply.name}: no charge_control* files")
    emit(f"saved={saved_limit() or 'none'}")

    emit("\n## write test")
    battery = find_battery()
    if not supported(battery):
        emit(f"result=unsupported ({ATTR} missing or not writable)")
        return 1
    original_end, original_start = thresholds(battery)
    if original_end is None:
        emit(f"result=failed (could not read {ATTR})")
        return 1
    if value is None:
        # Pick a value that differs from the current one so the write is observable.
        value = 75 if original_end == 80 else 80
    emit(f"battery={battery.name} original_end={original_end} original_start={show(original_start)} requested={value}")
    status_code = 0
    try:
        applied = apply(value, battery)
        differs = "" if applied == value else " (differs: clamped, rounded or rejected by the firmware)"
        emit(f"read_back_end={applied}{differs} read_back_start={show(thresholds(battery)[1])}")
    except RuntimeError as error:
        emit(f"write_error={error}")
        status_code = 1
    finally:
        status_code = probe_restore(battery, original_end, original_start, emit) or status_code
    if start_path(battery):
        emit("note=qcom_battmgr cannot turn the firmware limit off; it stays enabled at the restored thresholds")
    emit(f"result={'ok' if status_code == 0 else 'failed'}")
    return status_code


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="armada-charge-limit-probe",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Battery charge limit diagnostic. Prints the battery's charge_control* files,\n"
            "temporarily writes a test value to the charger, reads it back and then\n"
            "restores the original thresholds. Some drivers (qcom_battmgr) cannot turn\n"
            "the firmware limit off, so it may stay enabled afterwards, at 100% if no\n"
            "limit was set before:\n"
            "sudo armada-charge-limit-probe > charge-limit.txt"
        ),
    )
    parser.add_argument("--value", type=int, help=f"Test value to write ({MIN_LIMIT}-{MAX_LIMIT}, step {STEP})")
    args = parser.parse_args(argv)
    if args.value is not None:
        try:
            validate(args.value)
        except ValueError:
            parser.error(f"--value must be {MIN_LIMIT}-{MAX_LIMIT} in steps of {STEP}")
    if os.geteuid() != 0 and os.environ.get("ARMADA_CHARGE_LIMIT_ALLOW_NONROOT") != "1":
        parser.error("Run this command with sudo")
    return probe(args.value)


if __name__ == "__main__":
    sys.exit(main())
