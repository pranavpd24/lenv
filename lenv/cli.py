import argparse
from .core import LENV
import sys

def main():
    
    parser = argparse.ArgumentParser(
        description="LENV - Project-scoped Linux environments",
        epilog="For more info: https://github.com/pranavpd24/lenv"
    )
    
    subparsers = parser.add_subparsers(dest='command', help='Commands')
    
    # lenv init
    init_parser = subparsers.add_parser(
        'init', 
        help='Initialize environment (auto-installs WSL2 if needed)'
    )
    init_parser.add_argument(
        '--distro',
        choices=['alpine', 'ubuntu'],
        default='alpine',
        help='Linux distribution (default: alpine)'
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
    
    args = parser.parse_args()
    
    try:
        env = LENV()
        
        if args.command == 'init':
            env.init(distro=args.distro)
        elif args.command == 'activate':
            env.activate()
        elif args.command == 'run':
            command = ' '.join(args.cmd)
            env.run(command)
        elif args.command == 'destroy':
            env.destroy()
        elif args.command == 'status':
            env.status()
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




