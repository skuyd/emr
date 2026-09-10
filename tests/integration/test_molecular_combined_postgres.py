"""Real commits in combined output and the short final choice guard."""
from concurrent.futures import ThreadPoolExecutor
from queue import Queue
import time

import pytest
from django.db import connection

from apps.exports import services,views,choice_guards
from apps.operations.models import AuditEvent
from tests.exports.test_molecular_combined_domains import four_domains,change_domain
from tests.documents.fakes import InMemoryObjectStore
from tests.exports.test_lesion_output_bindings import authenticated
from tests.integration.test_family_postgres_concurrency import backend_pid,thread_call

pytestmark=[pytest.mark.postgres,pytest.mark.django_db(transaction=True)]


def require_postgres():
    if connection.vendor!="postgresql":pytest.skip("Requires isolated PostgreSQL")


@pytest.mark.parametrize("domain",["molecular","cancer","lesion","cloud"])
def test_each_committed_domain_change_stops_four_domain_stream(django_user_model,monkeypatch,record_property,domain):
    require_postgres()
    patient,_,lesion,cloud,scope,metric,_=four_domains(django_user_model)
    client=authenticated(patient)
    job=services.create_preview(patient,client.session.session_key,scope,actor=patient.account)
    services.request_generation(patient,client.session.session_key,job.pk,{"format":"json"},dispatch=lambda *_:None,actor=patient.account)
    store=InMemoryObjectStore();services.generate_export(job.pk,store)
    monkeypatch.setattr(views,"get_object_store",lambda:store)
    response=client.get(f"/visit/{job.pk}/download/",{"patient":str(patient.pk)})
    assert response.status_code==200
    response.block_size=128;stream=iter(response.streaming_content)
    assert len(next(stream))==128
    def change():
        assert not connection.in_atomic_block
        change_domain(patient,lesion,cloud,metric,domain)
        assert not connection.in_atomic_block
    pids=Queue();reader=backend_pid()
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(thread_call,change,pids);writer=pids.get(timeout=10)
            assert writer!=reader;future.result(timeout=30)
        record_property("reader_backend_pid",reader);record_property("writer_backend_pid",writer)
        assert b"".join(stream)==b""
    finally:response.close()
    job.refresh_from_db()
    assert job.status=="INVALIDATED" and job.snapshot=={} and not job.cloud_sources.exists()
    assert AuditEvent.objects.filter(action="export_downloaded",result="denied").count()==1


@pytest.mark.parametrize("route",["export","share"])
@pytest.mark.parametrize("order",["committed_before_final","waits_during_final"])
def test_final_choice_guard_serializes_actual_molecular_commit(django_user_model,monkeypatch,record_property,route,order):
    require_postgres()
    from apps.patients import share_views
    patient,_,lesion,cloud,_,metric,_=four_domains(django_user_model)
    client=authenticated(patient);pids=Queue();reader=backend_pid()
    observed={"rendered":False,"started":False,"blocked":False};futures=[]
    module=views if route=="export" else share_views
    template="exports/prepare.html" if route=="export" else "patients/shares.html"
    render=module.render;stamp=choice_guards.source_stamp
    def change():
        assert not connection.in_atomic_block
        change_domain(patient,lesion,cloud,metric,"molecular")
        assert not connection.in_atomic_block
    with ThreadPoolExecutor(max_workers=1) as pool:
        def start():
            observed["started"]=True
            futures.append(pool.submit(thread_call,change,pids))
            writer=pids.get(timeout=10);assert writer!=reader
            record_property("writer_backend_pid",writer)
            return writer
        def captured(*args,**kwargs):
            response=render(*args,**kwargs)
            if args[1]==template:
                assert "01.20" in response.content.decode()
                observed["rendered"]=True
                if order=="committed_before_final":start();futures[0].result(timeout=30)
            return response
        def final_stamp(*args,**kwargs):
            if observed["rendered"] and not observed["started"]:
                assert connection.in_atomic_block
                writer=start();deadline=time.monotonic()+10
                # Observe PostgreSQL's actual wait, rather than guessing with a sleep.
                while time.monotonic()<deadline:
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT %s = ANY(pg_blocking_pids(%s))",[reader,writer])
                        state=cursor.fetchone()
                    if state and state[0]:observed["blocked"]=True;break
                    if futures[0].done():break
                assert observed["blocked"] and not futures[0].done()
            return stamp(*args,**kwargs)
        monkeypatch.setattr(module,"render",captured)
        monkeypatch.setattr(choice_guards,"source_stamp",final_stamp)
        response=client.get("/visit/" if route=="export" else f"/patients/{patient.pk}/shares/",{"patient":str(patient.pk)})
        assert observed["rendered"] and observed["started"]
        if order=="committed_before_final":
            assert response.status_code in (409,410) and "01.20" not in response.content.decode()
        else:
            assert observed["blocked"] and response.status_code==200
        futures[0].result(timeout=30)
    record_property("reader_backend_pid",reader);record_property("commit_order",order)
