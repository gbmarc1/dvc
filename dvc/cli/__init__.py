"""This module provides an entrypoint to the dvc cli and parsing utils."""

import logging
import sys

# Workaround for CPython bug. See [1] and [2] for more info.
# [1] https://github.com/aws/aws-cli/blob/1.16.277/awscli/clidriver.py#L55
# [2] https://bugs.python.org/issue29288
from typing import Optional

"".encode("idna")

logger = logging.getLogger("dvc")


class DvcParserError(Exception):
    """Base class for CLI parser errors."""

    def __init__(self):
        super().__init__("parser error")


def parse_args(argv=None):
    """Parses CLI arguments.

    Args:
        argv: optional list of arguments to parse. sys.argv is used by default.

    Raises:
        DvcParserError: raised for argument parsing errors.
    """
    from .parser import get_main_parser

    parser = get_main_parser()
    args = parser.parse_args(argv)
    args.parser = parser
    return args


def _log_unknown_exceptions() -> None:
    from dvc.info import get_dvc_info
    from dvc.logger import FOOTER

    logger.exception("unexpected error")
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("Version info for developers:\n%s", get_dvc_info())
    logger.info(FOOTER)
    return None


def _log_exceptions(exc: Exception) -> Optional[int]:
    """Try to log some known exceptions, that are not DVCExceptions."""
    from dvc.utils import error_link, format_link

    if isinstance(exc, OSError):
        import errno

        if exc.errno == errno.EMFILE:
            logger.exception(
                "too many open files, please visit "
                "{} to see how to handle this "
                "problem".format(error_link("many-files")),
                extra={"tb_only": True},
            )
        else:
            _log_unknown_exceptions()
        return None

    from dvc.fs import AuthError, ConfigError, RemoteMissingDepsError

    if isinstance(exc, RemoteMissingDepsError):
        from dvc.utils.pkg import PKG

        proto = exc.protocol
        by_pkg = {
            "pip": f"pip install 'dvc[{proto}]'",
            "conda": f"conda install -c conda-forge dvc-{proto}",
        }

        cmd = by_pkg.get(PKG)
        if cmd:
            link = format_link("https://dvc.org/doc/install")
            hint = (
                f"To install dvc with those dependencies, run:\n"
                "\n"
                f"\t{cmd}\n"
                "\n"
                f"See {link} for more info."
            )
        else:
            link = format_link("https://github.com/iterative/dvc/issues")
            hint = f"\nPlease report this bug to {link}. Thank you!"

        logger.exception(
            f"URL '{exc.url}' is supported but requires these missing "
            f"dependencies: {exc.missing_deps}. {hint}",
            extra={"tb_only": True},
        )
        return None

    if isinstance(exc, (AuthError, ConfigError)):
        link = format_link("https://man.dvc.org/remote/modify")
        logger.exception("configuration error")
        logger.exception(
            f"{exc!s}\nLearn more about configuration settings at {link}.",
            extra={"tb_only": True},
        )
        return 251

    from dvc_data.hashfile.cache import DiskError

    if isinstance(exc, DiskError):
        from dvc.utils import relpath

        directory = relpath(exc.directory)
        logger.exception(
            f"Could not open pickled '{exc.type}' cache.\n"
            f"Remove the '{directory}' directory and then retry this command."
            f"\nSee {error_link('pickle')} for more information.",
            extra={"tb_only": True},
        )
        return None

    from dvc_data.build import IgnoreInCollectedDirError

    if isinstance(exc, IgnoreInCollectedDirError):
        logger.exception("")
        return None

    _log_unknown_exceptions()
    return None


def main(argv=None):  # noqa: C901
    """Main entry point for dvc CLI.

    Args:
        argv: optional list of arguments to parse. sys.argv is used by default.

    Returns:
        int: command's return code.
    """
    from dvc._debug import debugtools
    from dvc.config import ConfigError
    from dvc.exceptions import DvcException, NotDvcRepoError
    from dvc.logger import disable_other_loggers, set_loggers_level

    # NOTE: stderr/stdout may be closed if we are running from dvc.daemon.
    # On Linux we directly call cli.main after double forking and closing
    # the copied parent's standard file descriptors. If we make any logging
    # calls in this state it will cause an exception due to writing to a closed
    # file descriptor.
    if sys.stderr.closed:  # pylint: disable=using-constant-test
        logging.disable()
    elif sys.stdout.closed:  # pylint: disable=using-constant-test
        logging.disable(logging.INFO)

    args = None
    disable_other_loggers()

    outerLogLevel = logger.level
    try:
        args = parse_args(argv)

        level = None
        if args.quiet:
            level = logging.CRITICAL
        elif args.verbose == 1:
            level = logging.DEBUG
        elif args.verbose > 1:
            level = logging.TRACE

        if level is not None:
            set_loggers_level(level)

        logger.trace(args)

        if not sys.stdout.closed and not args.quiet:
            from dvc.ui import ui

            ui.enable()

        with debugtools(args):
            cmd = args.func(args)
            ret = cmd.do_run()
    except ConfigError:
        logger.exception("configuration error")
        ret = 251
    except KeyboardInterrupt:
        logger.exception("interrupted by the user")
        ret = 252
    except NotDvcRepoError:
        logger.exception("")
        ret = 253
    except DvcException:
        ret = 255
        logger.exception("")
    except DvcParserError:
        ret = 254
    except Exception as exc:  # noqa, pylint: disable=broad-except
        # pylint: disable=no-member
        ret = _log_exceptions(exc) or 255

    try:
        from dvc import analytics

        if analytics.is_enabled():
            analytics.collect_and_send_report(args, ret)

        return ret
    finally:
        logger.setLevel(outerLogLevel)

        from dvc.external_repo import clean_repos

        # Remove cached repos in the end of the call, these are anonymous
        # so won't be reused by any other subsequent run anyway.
        clean_repos()
