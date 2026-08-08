import argparse
from .core import LENV
import sys
from . import __version__

def _print_full_help(parser, subparsers):
    """Top-level help followed by every subcommand's full option list,
    so a bare `lenv` shows everything the CLI can do."""
    parser.print_help()
    print("\n" + "=" * 60)
    print("Options for each command")
    print("=" * 60)
    for name, sub in subparsers.choices.items():
        print()
        sub.print_help()

def main():
    
    parser = argparse.ArgumentParser(
        description="LENV - Project-scoped Linux environments",
        epilog="For more info: https://github.com/pranavpd24/lenv"
    )
    parser.add_argument(
        '--version', 
        action='version', 
        version=f'%(prog)s {__version__}'
    )
    
    subparsers = parser.add_subparsers(dest='command', help='Commands')
    
    # lenv init
    init_parser = subparsers.add_parser(
        'init', 
        help='Initialize environment (auto-installs WSL2 if needed)'
    )
    init_parser.add_argument(
        '--distro',
        choices= ['alpine', 'ubuntu'],
        default=None,
        help='Linux distribution (skips the interactive prompt)'
    )
    init_parser.add_argument(
        '--rootfs',
        metavar='PATH',
        default=None,
        help='Path to a custom rootfs tarball (skips prompt and download)'
    )
    init_parser.add_argument(
        '--build',
        metavar='NAME',
        default=None,
        help='Install a bundled package set from lenv/builds/<name>.yaml'
    )
    
    # lenv activate
    subparsers.add_parser('activate', help='Enter Linux environment')
    
    # lenv run
    run_parser = subparsers.add_parser('run', help='Run command')
    run_parser.add_argument('cmd', nargs='+')
    
    # lenv destroy
    destroy_parser = subparsers.add_parser('destroy', help='Remove environment')
    destroy_parser.add_argument(
        '--yes', '-y',
        action='store_true',
        help='Skip the confirmation prompt'
    )
    
    # lenv status
    subparsers.add_parser('status', help='Show environment status')

    # lenv list
    subparsers.add_parser('list', help='List all lenv environments on this machine')

    # lenv compact
    compact_parser = subparsers.add_parser(
        'compact',
        help='Reclaim disk space from the environment (rebuilds the virtual disk)'
    )
    compact_parser.add_argument(
        '--yes', '-y',
        action='store_true',
        help='Skip the confirmation prompt'
    )

    
    args = parser.parse_args()
    
    try:
        if args.command is None:
            _print_full_help(parser, subparsers)
            return

        if args.command == 'init':
            env = LENV(distro_set=args.distro, rootfs_path=args.rootfs, build=args.build)
            env.init()
        else:
            env = LENV()
    
            if args.command == 'activate':
                env.activate()
            elif args.command == 'run':
                command = ' '.join(args.cmd)
                sys.exit(env.run(command))
            elif args.command == 'destroy':
                env.destroy(assume_yes=args.yes)
            elif args.command == 'status':
                env.status()
            elif args.command == 'list':
                env.list_instances()
            elif args.command == 'compact':
                env.compact(assume_yes=args.yes)
            else:
                _print_full_help(parser, subparsers)

    
    except KeyboardInterrupt:
        print("\n\n  Interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n Error: {e}")
        sys.exit(1)
    
if __name__ == '__main__':
    main()



