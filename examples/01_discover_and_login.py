"""Example 1: can we talk to the NAS?

SYNO.API.Info (discovery) -> SYNO.API.Auth login -> SYNO.FileStation.Info get -> logout.
"""

from synology_poc import Settings, connect
from synology_poc.config import mask


def main() -> None:
    settings = Settings.from_env()
    print(f"Connecting to https://{settings.host}:{settings.port} as {settings.user}")

    with connect(settings) as client:
        print("\n[1] SYNO.API.Info: File Station APIs advertised by this NAS")
        print(f"    {'API':<40} {'path':<14} versions")
        for name in sorted(client.apis):
            if name.startswith("SYNO.FileStation.") or name == "SYNO.API.Auth":
                info = client.apis[name]
                print(f"    {name:<40} {info['path']:<14} {info['minVersion']}-{info['maxVersion']}")

        print(f"\n[2] SYNO.API.Auth login (v{client.version('SYNO.API.Auth')}) OK, sid={mask(client.sid)}")

        info = client.call("SYNO.FileStation.Info", "get")
        print("\n[3] SYNO.FileStation.Info.get")
        print(f"    hostname        : {info.get('hostname')}")
        print(f"    is_manager      : {info.get('is_manager')}  (expected False for a test user)")
        print(f"    support_sharing : {info.get('support_sharing')}")
        print(f"    virtual FS      : {info.get('support_virtual_protocol')}")

    print("\n[4] Logged out.")


if __name__ == "__main__":
    main()
