import datetime
import shlex
import subprocess

from . import (
    config, constants, directories, exceptions, logging, rotators, targets)

logger = logging.getLogger(__name__)


def backup_directory(
    directory: directories.Directory,
    *,
    config: config.Config,
    timestamp: datetime.datetime,
    dry_run: bool = False,
    send_files: bool = True,
    create_snapshot: bool = True,
    rotate_snapshot: bool = True,
) -> None:
    """
    Backup a directory.

    `timestamp` is the datetime to use for the backup timestamp.
    It should be timezone aware.

    If `dry_run` is True, no changes will be made, but the backup will be simulated.
    If `verbose` is True, `rsync` will be run with '-v' flag.
    If `send_files` is True (the default), files will be copied to the backup destination.
    If `create_snapshot` is True (the default), a new timestamped snapshot directory will be create.
    If `rotate_snapshot` is True (the default), old snapshots will be rotated
    and possibly deleted if they are no longer required.
    """
    with directory.target.connect() as context:
        backup_directory_with_context(
            directory, context, config=config, timestamp=timestamp, dry_run=dry_run,
            send_files=send_files, create_snapshot=create_snapshot, rotate_snapshot=rotate_snapshot)


def backup_directory_with_context(
    directory: directories.Directory,
    context: targets.TargetContext,
    *,
    config: config.Config,
    timestamp: datetime.datetime,
    dry_run: bool = False,
    send_files: bool = True,
    create_snapshot: bool = True,
    rotate_snapshot: bool = True,
) -> None:
    logger.log(logging.MESSAGE, "Backing up %s", directory)
    if send_files:
        _send_files(
            directory, context, config=config, dry_run=dry_run)

    if create_snapshot:
        _create_snapshot(
            directory, context, dry_run=dry_run, timestamp=timestamp)

    if rotate_snapshot:
        _rotate_snapshot(
            directory, context, dry_run=dry_run, timestamp=timestamp)


def _send_files(
    directory: directories.Directory,
    context: targets.TargetContext,
    *,
    config: config.Config,
    dry_run: bool,
) -> None:
    """Copy files from the source to the target using rsync."""
    # The following flags are inspired by python-rsync-system-backup
    command = ['rsync']

    command.append('--human-readable')
    verbosity = config.get(logging.Verbosity)
    if verbosity is logging.Verbosity.all:
        # Turn on fairly verbose logging for rsync
        command.append('--verbose')
    elif verbosity is logging.Verbosity.silent:
        # rsync is silent by default
        pass
    else:
        # Some minimal rsync output
        command.append('--info=progress2,stats')

    if dry_run:
        command.append('--dry-run')

    # The following rsync options delete files in the backup
    # destination that no longer exist on the local system.
    # Due to snapshotting this won't cause data loss.
    command.append('--delete-after')
    command.append('--delete-excluded')

    # The following rsync options are intended to preserve
    # as much filesystem metadata as possible.
    command.append('--acls')
    command.append('--archive')
    command.append('--hard-links')
    command.append('--fuzzy')
    command.append('--fuzzy')
    command.append('--numeric-ids')
    command.append('--xattrs')

    # The following rsync option avoids including mounted external
    # drives like USB sticks in system backups.
    if directory.one_file_system:
        command.append('--one-file-system')

    # The following rsync options allow user defined exclusion.
    if (exclude_from := directory.resolve_exclude_from()) is not None:
        command.append('--exclude-from=%s' % exclude_from)
    for pattern in directory.exclude_files:
        command.append('--exclude=%s' % pattern)

    current_path = context.make_path(directory.target_path / constants.CURRENT_SNAPSHOT_NAME)
    target_str, target_arguments = directory.target.rsync_arguments(current_path)

    command.extend(target_arguments)
    command.append(_ensure_trailing_slash(str(directory.source_path)))
    command.append(_ensure_trailing_slash(target_str))

    if not context.exists(directory.target_path):
        logger.log(logging.INFO, "Creating destination directory %r", directory.target_path)
        cmd = ["mkdir", "-p", str(context.make_path(directory.target_path))]
        logger.log(logging.DEBUG, "remote $ %s", shlex.join(cmd))
        if not dry_run:
            context.execute(cmd)

    logger.log(logging.INFO, "Running rsync")
    logger.log(logging.DEBUG, "$ %s", shlex.join(command))

    # Execute the rsync command.
    result = subprocess.run(command, check=False)

    # From `man rsync':
    #  - 23: Partial transfer due to error.
    #  - 24: Partial transfer due to vanished source files.
    # This can be expected on a running system
    # without proper filesystem snapshots :-).
    if result.returncode in (0, 23, 24):
        logger.log(logging.INFO, "Finished backup")
        if result.returncode != 0:
            logger.log(
                logging.WARNING,
                "Ignoring `partial transfer' warnings (rsync exited with %i).",
                result.returncode,
            )
    else:
        logger.log(logging.ERROR, "Backup failed! (rsync exited with %i)", result.returncode)
        raise exceptions.RsyncError("rsync call failed", result.returncode)


def _create_snapshot(
    directory: directories.Directory,
    context: targets.TargetContext,
    *,
    timestamp: datetime.datetime,
    dry_run: bool,
) -> None:
    """
    Create a snapshot of the current backup for this Directory.
    """
    target_directory = directory.target_path
    snapshot_name = _snapshot_name(directory, timestamp)
    current = target_directory / constants.CURRENT_SNAPSHOT_NAME
    snapshot = target_directory / snapshot_name
    logger.log(logging.INFO, "Creating snapshot %s", snapshot_name)

    cmd = [
        "cp", "--archive", "--link", "--no-target-directory", "--force",
        str(context.make_path(current)), str(context.make_path(snapshot)),
    ]
    logger.log(logging.DEBUG, "remote $ %s", shlex.join(cmd))
    if not dry_run:
        context.execute(cmd)
        context.write_file(
            snapshot / constants.TIMESTAMP_FILE_NAME,
            timestamp.isoformat().encode())


def _rotate_snapshot(
    directory: directories.Directory,
    context: targets.TargetContext,
    *,
    timestamp: datetime.datetime,
    dry_run: bool,
) -> None:
    """
    Rotate the existing backups using the defined keeper for this Directory.
    """
    if directory.rotate is None:
        logger.log(logging.INFO, "Not rotating backups: no rotator configured")
        return

    if (reason := directory.rotate.should_rotate()) is not True:
        logger.log(logging.INFO, f"Not rotating backups: {reason}")
        return

    logger.log(logging.INFO, f"Rotating backups using '{directory.rotate}' strategy")
    backups = list(context.list_backups(directory.target_path))

    # If this is a dry run, a current snapshot will not have been made.
    # To simulate the backup process properly, append a fictitious snapshot
    # that would have been created in a normal run
    if dry_run:
        backups.append(targets.Backup(
            name=_snapshot_name(directory, timestamp),
            timestamp=timestamp))

    results = directory.rotate.rotate_backups(timestamp, backups)
    for backup, verdict, explanation in sorted(results):
        logger.log(logging.INFO, f"  - {backup.name}: {verdict.name}. {explanation}")
        if not dry_run and verdict is rotators.Verdict.drop:
            entry_path = context.make_path(directory.target_path / backup.name)
            context.execute(["chmod", "-R", "u+wX", str(entry_path)])
            context.execute(["rm", "-rf", str(entry_path)])


def _snapshot_name(directory: directories.Directory, timestamp: datetime.datetime) -> str:
    """
    Convert a timestamp into a snapshot directory name.
    This should include the timestamp to make it unique,
    but the timestamp is never parsed from this filename.
    """
    return timestamp.strftime("snapshot-%Y-%m-%dT%H:%M:%S")


def _ensure_trailing_slash(path: str) -> str:
    """Ensure a path ends with a slash."""
    if not path.endswith('/'):
        return path + '/'
    return path
