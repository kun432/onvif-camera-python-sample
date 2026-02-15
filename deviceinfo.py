import asyncio
import os
import onvif
from dotenv import load_dotenv

async def main():
    load_dotenv()
    host = os.environ["ONVIF_HOST"]
    port = int(os.environ.get("ONVIF_PORT", "80"))
    user = os.environ["ONVIF_USER"]
    password = os.environ["ONVIF_PASSWORD"]

    wsdl_dir = f"{os.path.dirname(onvif.__file__)}/wsdl/"
    cam = onvif.ONVIFCamera(host, port, user, password, wsdl_dir=wsdl_dir)

    try:
        await cam.update_xaddrs()

        device = await cam.create_devicemgmt_service()
        info = await device.GetDeviceInformation()

        print("== DeviceInformation ==")
        print(f"Manufacturer: {getattr(info, 'Manufacturer', None)}")
        print(f"Model       : {getattr(info, 'Model', None)}")
        print(f"Firmware    : {getattr(info, 'FirmwareVersion', None)}")

    finally:
        await cam.close()

if __name__ == "__main__":
    asyncio.run(main())

