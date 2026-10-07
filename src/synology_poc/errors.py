"""Error codes from docs/synology/file-station-api.md (sections 4 and 6)."""

COMMON = {
    100: "Unknown error",
    101: "No parameter of API, method or version",
    102: "The requested API does not exist",
    103: "The requested method does not exist",
    104: "The requested version does not support the functionality",
    105: "The logged in session does not have permission",
    106: "Session timeout",
    107: "Session interrupted by duplicate login",
    119: "SID not found",
}

FILE_STATION = {
    400: "Invalid parameter of file operation",
    401: "Unknown error of file operation",
    402: "System is too busy",
    403: "Invalid user does this file operation",
    404: "Invalid group does this file operation",
    405: "Invalid user and group does this file operation",
    406: "Can't get user/group information from the account server",
    407: "Operation not permitted",
    408: "No such file or directory",
    409: "Non-supported file system",
    410: "Failed to connect internet-based file system (e.g., CIFS)",
    411: "Read-only file system",
    412: "Filename too long in the non-encrypted file system",
    413: "Filename too long in the encrypted file system",
    414: "File already exists",
    415: "Disk quota exceeded",
    416: "No space left on device",
    417: "Input/output error",
    418: "Illegal name or path",
    419: "Illegal file name",
    420: "Illegal file name on FAT file system",
    421: "Device or resource busy",
    599: "No such task of the file operation",
}

API_SPECIFIC = {
    "SYNO.API.Auth": {
        400: "No such account or incorrect password",
        401: "Account disabled",
        402: "Permission denied",
        403: "2-step verification code required",
        404: "Failed to authenticate 2-step verification code",
    },
    "SYNO.FileStation.CreateFolder": {
        1100: "Failed to create a folder",
        1101: "The number of folders in the parent folder would exceed the system limitation",
    },
    "SYNO.FileStation.Rename": {
        1200: "Failed to rename it",
    },
    "SYNO.FileStation.CopyMove": {
        1000: "Failed to copy files/folders",
        1001: "Failed to move files/folders",
        1002: "An error occurred at the destination",
        1003: "Cannot overwrite or skip the existing file because no overwrite parameter is given",
        1004: "File cannot overwrite a folder with the same name, or vice versa",
        1006: "Cannot copy/move file/folder with special characters to a FAT32 file system",
        1007: "Cannot copy/move a file bigger than 4G to a FAT32 file system",
    },
    "SYNO.FileStation.Delete": {
        900: "Failed to delete file(s)/folder(s)",
    },
    "SYNO.FileStation.Upload": {
        1800: "Content-Length missing or does not match the received size",
        1801: "Timed out waiting for data from the client",
        1802: "No filename information in the last part of file content",
        1803: "Upload connection is cancelled",
        1804: "Failed to upload oversized file to FAT file system",
        1805: "Can't overwrite or skip the existing file, no overwrite parameter given",
    },
    "SYNO.FileStation.Compress": {
        1300: "Failed to compress files/folders",
        1301: "Cannot create the archive because the given archive name is too long",
    },
    "SYNO.FileStation.Extract": {
        1400: "Failed to extract files",
        1401: "Cannot open the file as archive",
        1402: "Failed to read archive data",
        1403: "Wrong password",
        1404: "Failed to get the file and dir list in an archive",
        1405: "Failed to find the item ID in an archive file",
    },
    "SYNO.FileStation.Sharing": {
        2000: "Sharing link does not exist",
        2001: "Cannot generate sharing link because too many sharing links exist",
        2002: "Failed to access sharing links",
    },
}


def describe(api: str, code: int) -> str:
    """Resolve a code: API-specific first, then File Station common, then WebAPI common."""
    specific = API_SPECIFIC.get(api, {})
    if code in specific:
        return specific[code]
    if api.startswith("SYNO.FileStation.") and code in FILE_STATION:
        return FILE_STATION[code]
    return COMMON.get(code, "Undocumented error code")


class SynologyError(Exception):
    """A WebAPI call returned success=false."""

    def __init__(
        self,
        api: str,
        method: str,
        code: int,
        errors: list | None = None,
        *,
        http_status: bool = False,
    ):
        self.api = api
        self.method = method
        self.code = code
        self.errors = errors or []
        self.http_status = http_status
        # Binary endpoints (Thumb, Download with mode=open) report errors as HTTP statuses.
        self.message = f"HTTP {code}" if http_status else describe(api, code)
        detail = ""
        if self.errors:
            parts = []
            for e in self.errors:
                where = e.get("path") or e.get("name") or ""
                parts.append(f"{e.get('code')} {describe(api, e.get('code', 0))} {where}".strip())
            detail = " [" + "; ".join(parts) + "]"
        super().__init__(f"{api}.{method} -> {code}: {self.message}{detail}")
