from .server import serve

def main() -> None:
    from .config import load
    serve(load())

if __name__ == "__main__":
    main()
