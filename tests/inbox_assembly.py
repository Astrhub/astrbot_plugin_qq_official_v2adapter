import copy
import json
import time

from v2.models import InstanceKey
from v2.protocol import RawEnvelope


async def exercise_inbox_recovery(owner, instance, client, prefix, headers, boot, api_key, auth):
    inbox, key = owner.inbox, instance.identity.settings_key
    configs = owner.context.get_config()["platform"]
    original_secret = configs[0]["secret"]
    other = {**copy.deepcopy(configs[0]), "id": "recovery-other", "appid": "other-app", "enable": False}
    configs.append(other)
    other_key = InstanceKey.from_config(other).settings_key
    try:
        for scope in (key, other_key):
            for number in range(2):
                payload = {"op": 0, "id": f"recovery-{number}", "t": "UNSUPPORTED_EVENT", "d": {"token": "fixture-retained-private-body"}}
                assert inbox.accept(scope, RawEnvelope(payload, time.time()))
                receipt = inbox.pending(scope)[0]["receipt"]
                inbox.retain(scope, receipt, "fixture_unknown", invalid=number == 1)
        pending = {"op": 0, "id": "do-not-discard-pending", "t": "GROUP_MESSAGE_CREATE", "d": {}}
        inbox.accept(key, RawEnvelope(pending, time.time()))
        route = prefix + "/inbox/retained"
        params = {"platform_id": instance.identity.platform_id}
        assert (await client.get(route, params=params)).status_code == 401
        for bad_headers in ({"Authorization": "ApiKey " + api_key}, auth(username="another"), {**headers, "Origin": "https://evil.invalid"}):
            assert (await client.get(route, params=params, headers=bad_headers)).status_code in (401, 403)
        response = await client.get(route, params=params, headers=headers)
        assert response.status_code == 200, response.text
        data = response.json()
        assert len(data["entries"]) == 2 and data["counts"] == {"extension": 1, "invalid": 1, "pending": 1}
        assert "fixture-retained-private-body" not in response.text and '"payload"' not in response.text
        body = {**params, "fingerprint": data["fingerprint"], "csrf": boot["csrf"], "confirm": True,
                "entries": data["entries"], "expires": data["expires"]}
        route = prefix + "/inbox/discard"
        for bad_headers in ({}, {"Authorization": "ApiKey " + api_key}, {**headers, "Origin": "https://evil.invalid"}):
            assert (await client.post(route, json=body, headers=bad_headers)).status_code in (401, 403)
        for patch, status in [({"csrf": "bad"}, 403), ({"confirm": False}, 400), ({"fingerprint": "stale"}, 409),
                              ({"expires": 0}, 409), ({"entries": []}, 400)]:
            response = await client.post(route, json={**body, **patch}, headers=headers)
            assert response.status_code == status, response.text
            assert inbox.count(key) == 3
        tampered = copy.deepcopy(body)
        tampered["entries"][0]["receipt"] += 100
        assert (await client.post(route, json=tampered, headers=headers)).status_code == 409
        other_view = (await client.get(prefix + "/inbox/retained", params={"platform_id": other["id"]}, headers=headers)).json()
        assert (await client.post(route, json={**body, "platform_id": other["id"], "fingerprint": other_view["fingerprint"]}, headers=headers)).status_code == 409
        assert inbox.count(other_key) == 2
        configs[0]["secret"] += "-changed"
        assert (await client.post(route, json=body, headers=headers)).status_code == 409
        configs[0]["secret"] = configs[0]["secret"].removesuffix("-changed")
        inbox.retain(key, data["entries"][1]["receipt"], "changed_reason", invalid=True)
        assert (await client.post(route, json=body, headers=headers)).status_code == 409
        assert inbox.count(key) == 3
        data = (await client.get(prefix + "/inbox/retained", params=params, headers=headers)).json()
        body.update(entries=data["entries"], expires=data["expires"])
        response = await client.post(route, json=body, headers=headers)
        assert response.status_code == 200 and response.json()["discarded"] == 2, response.text
        assert inbox.count(key) == 1 and inbox.pending(key)[0]["payload"] == pending
        assert inbox.count(other_key) == 2
        assert (await client.post(route, json=body, headers=headers)).status_code == 409
        assert "fixture-retained-private-body" not in json.dumps(response.json())
    finally:
        # Remove only fixtures so later lifecycle/message assembly retains its original baseline.
        with inbox.db:
            inbox.db.execute("DELETE FROM inbox WHERE owner IN (?,?) AND event_id IN ('recovery-0','recovery-1','do-not-discard-pending')", (key, other_key))
        configs[0]["secret"] = original_secret
        configs.remove(other)
