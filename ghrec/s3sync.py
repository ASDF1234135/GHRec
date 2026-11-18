"""Mirror DATA_ROOT with s3://DATA_BUCKET so the stateless Fargate job sees yesterday's data:
python -m ghrec.s3sync pull   (before the job)   |   python -m ghrec.s3sync push   (after it)
shortcut: compares file size only and walks the whole bucket; fine for a few GB, use prefixes/ETags beyond that."""
import os
import sys
from pathlib import Path


def _bucket():
    import boto3  # imported here so local runs and tests need no AWS SDK

    return boto3.resource("s3").Bucket(os.environ["DATA_BUCKET"]), Path(os.environ.get("DATA_ROOT", "/data"))


def pull() -> None:
    bucket, root = _bucket()
    for o in bucket.objects.all():
        dest = root / o.key
        if dest.exists() and dest.stat().st_size == o.size:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        bucket.download_file(o.key, str(dest))


def push() -> None:
    bucket, root = _bucket()
    remote = {o.key: o.size for o in bucket.objects.all()}
    for f in root.rglob("*"):
        key = f.relative_to(root).as_posix()
        if f.is_file() and remote.get(key) != f.stat().st_size:
            bucket.upload_file(str(f), key)


if __name__ == "__main__":
    {"pull": pull, "push": push}[sys.argv[1]]()
