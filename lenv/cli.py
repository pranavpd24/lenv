import argparse
from .core import LENV
import sys
from . import __version__

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
        help='Intialize environment (auto-installs WSL2 if needed)'
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
    
    # lenv activate
    subparsers.add_parser('activate', help='Enter Linux environment')
    
    # lenv run
    run_parser = subparsers.add_parser('run', help='Run command')
    run_parser.add_argument('cmd', nargs='+')
    
    # lenv destroy
    subparsers.add_parser('destroy', help='Remove environment')
    
    # lenv status
    subparsers.add_parser('status', help='Show environment status')

    # lenv list
    subparsers.add_parser('list', help='List all lenv environments on this machine')

    
    args = parser.parse_args()
    
    try:
        if args.command == 'init':
            env = LENV(distro_set=args.distro, rootfs_path=args.rootfs)
            env.init()
        else:
            env = LENV()
    
            if args.command == 'activate':
                env.activate()
            elif args.command == 'run':
                command = ' '.join(args.cmd)
                env.run(command)
            elif args.command == 'destroy':
                env.destroy()
            elif args.command == 'status':
                env.status()
            elif args.command == 'list':
                env.list_instances()
            else:
                parser.print_help()

    
    except KeyboardInterrupt:
        print("\n\n  Interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n Error: {e}")
        sys.exit(1)
    
if __name__ == '__main__':
    main()




