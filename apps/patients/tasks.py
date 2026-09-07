from celery import shared_task

from .sharing import expire_shares


@shared_task(name="patients.expire_shares")
def expire_shared_content():
    return {"count": expire_shares()}
