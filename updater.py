import requests
import tempfile
import uuid
import os
import utils
import random
import string
from pathlib import Path
import traceback


class ServerNotFound(Exception):
    pass


def get_latest_release():
    try:
        response = requests.get(
            "https://api.github.com/repos/GameFriendsTeam/UltraStream/releases")
        response.raise_for_status()
        releases = response.json()
        if releases:
            release = list(filter(
                lambda r: not r.get(
                    "prerelease",
                    False),
                releases))[0]
            if release:
                return (release.get("tag_name"), release.get("assets", []))
    except requests.RequestException as e:
        print(f"Error checking for updates: {e}")
        traceback.print_exception(e)
        return False


def get_asset_data(asset):
    return {"name": asset.get("name"),
            "download_url": asset.get("browser_download_url"),
            "digest": asset.get("digest"),
            "updated_at": asset.get("updated_at"),
            "content_type": asset.get("content_type"),
            "size": asset.get("size"),
            "uploader_login": asset.get("uploader",
                                        {"login": "unknown"}).get("login",
                                                                  "unknown")}


def check_update():
    try:
        from srv import get_version
    except ImportError:
        raise ServerNotFound("Please place this script in dir of server")
        
    latest_release = get_latest_release()
    if not latest_release:
        print("Failed to fetch the latest release information.")
        return False

    tag, assets = latest_release
    asset_d = get_asset_data(assets[0]) if assets else None
    if not asset_d:
        print("No assets found for the latest release.")
        return False

    current_version = get_version()
    major, minor, delta = map(int, current_version.split('.'))
    majorT, minorT, deltaT = map(int, tag.split('.'))
    if majorT > major or (majorT == major and minorT > minor) or (
            majorT == major and minorT == minor and deltaT > delta):
        print(f"Update available: {tag} (new: {tag}, current: {current_version})")
        return True
    else:
        return False


def prepare_updater(update_dir: str, target_zip: str, target_dir: str):
    pid = os.getpid()
#    parent_pid = os.getppid()

    cmd = utils.get_cmdline(pid)
#    pcmd = utils.get_cmdline(parent_pid)
    fname = f"{update_dir}/updater.py"
    mode = "w+" if os.path.exists(fname) else "w"
    std = os.stdout

    with open(fname, mode) as f:
        f.write(f"""import os, zipfile, shutil, time, subprocess, sys
print("Updater started...")
os.kill({pid}, 9)

time.sleep(0.5)

shutil.rmtree("{target_zip}", ignore_errors=True)
with zipfile.ZipFile("{target_zip}", 'r') as zip_ref:
    zip_ref.extractall("{target_dir}")

os.remove("{target_zip}")

platform = sys.platform

if (platform.startswith("linux") or platform.startswith('android') or
    platform == "darwin" or platform.startswith("freebsd")):
    subprocess.Popen({cmd}, start_new_session=True, stdout=)
    subprocess.Popen(['python3', '-c', '"import shutil;shutil.rmtree(\\"{update_dir}\\")"'], start_new_session=True)
elif platform == "win32":
    subprocess.Popen({cmd}, creationflags=subprocess.DETACHED_PROCESS)
    subprocess.Popen(['python', '-c', '"import shutil;shutil.rmtree(\\"{update_dir}\\")"'], creationflags=subprocess.DETACHED_PROCESS)

""")
    return fname


def update(release: tuple[str, list[dict]]):
    print("Updating...")
    tag, assets = release
    asset_d = get_asset_data(assets[0]) if assets else None
    if not asset_d:
        print("No assets found for the latest release.")
        return False

    name = asset_d.get('name', uuid.uuid4().hex)
    current_dir = str(Path.cwd())
    tmp_dir = tempfile.gettempdir()+"/"+"".join(random.choices(string.ascii_letters+string.digits, k=10))
    asset_path = f"{tmp_dir}/{name}"

    os.makedirs(tmp_dir, mode=775, exist_ok=True)
    updaterF = prepare_updater(tmp_dir, asset_path, current_dir)

    try:
        response = requests.get(asset_d.get("download_url"), stream=True)
        response.raise_for_status()
        with open(asset_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)

        utils.run_detached_process(["python", updaterF])
        return True
    except requests.RequestException as e:
        print(f"Error downloading the asset: {e}")
        return False


if __name__ == "__main__":
    if check_update():
        print("Update available!")
        i = True if input("Do you want to update now? (y/n): ").lower() == 'y' else False
        if i:
            update(get_latest_release())
    else:
        print("No update available.")
