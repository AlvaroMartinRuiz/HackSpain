import uvicorn

from v2.config import Config


def main():
    config = Config()
    uvicorn.run("v2.api:app", host=config.host, port=config.port, reload=False, ws_max_size=65536)


if __name__ == "__main__":
    main()
