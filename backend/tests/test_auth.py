"""Authentication and authorization tests (LLD 02 s10), with generated RSA keys and mock key servers."""
import asyncio
import base64
import json
import os
import sys
import time
import unittest
import uuid

try:
    import httpx
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    from fastapi.testclient import TestClient
except ImportError as exc:
    raise unittest.SkipTest(f"auth test extras missing: {exc}")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from studybuddy.api import create_app  # noqa: E402
from studybuddy.auth import (AuthUnavailable, BearerAuth, Facts, HttpJwks, InMemoryDenylist, InMemoryUserDirectory,  # noqa: E402
                             JwtVerifier, Principal, StaticJwks, authorize, initial_role)
from tests.test_api import make_orch, body  # noqa: E402

ISS, AUD = "https://idp.example/realms/studybuddy", "studybuddy-api"
TENANT_A, TENANT_B = str(uuid.uuid4()), str(uuid.uuid4())


def make_key(kid):
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(priv.public_key()))
    jwk.update(kid=kid, alg="RS256", use="sig")
    return priv, jwk


KEY, JWK = make_key("k1")
OTHER_KEY, _ = make_key("k1")  # same kid, different key: a forgery


def token(key=KEY, kid="k1", alg="RS256", **over):
    now = int(time.time())
    claims = {"iss": ISS, "aud": AUD, "sub": "alice", "iat": now, "exp": now + 600, "tenant_id": TENANT_A,
              "roles": ["student"], "jti": uuid.uuid4().hex}
    claims.update(over)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm=alg, headers={"kid": kid})


def run(coro):
    return asyncio.run(coro)


def verifier(jwks=None, **kw):
    return JwtVerifier(issuer=ISS, audience=AUD, jwks=jwks or StaticJwks([JWK]), **kw)


class VerifierTests(unittest.TestCase):
    def reject(self, tok, **kw):
        from studybuddy.auth import AuthError
        with self.assertRaises(AuthError):
            run(verifier(**kw).verify(tok))

    def test_valid_token_passes(self):
        self.assertEqual(run(verifier().verify(token()))["sub"], "alice")

    def test_expired_forged_wrong_audience_wrong_issuer_rejected(self):
        self.reject(token(exp=int(time.time()) - 600, iat=int(time.time()) - 1200))
        self.reject(token(key=OTHER_KEY))
        self.reject(token(aud="other-api"))
        self.reject(token(iss="https://evil.example"))
        self.reject(token(exp=None))
        self.reject(token(sub=None))

    def test_clock_skew_within_leeway_ok_beyond_rejected(self):
        now = int(time.time())
        self.assertTrue(run(verifier().verify(token(iat=now - 700, exp=now - 30))))
        self.reject(token(iat=now - 700, exp=now - 300))
        self.reject(token(nbf=now + 600))

    def test_algorithm_confusion_and_none_rejected(self):
        self.reject(jwt.encode({"iss": ISS, "aud": AUD, "sub": "a", "iat": 1, "exp": 4102444800}, key="", algorithm="none", headers={"kid": "k1"}))
        # attacker signs HS256 with the public key text as the secret; built by hand because PyJWT refuses to
        b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
        import hashlib, hmac
        head = b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": "k1"}).encode())
        pay = b64(json.dumps({"iss": ISS, "aud": AUD, "sub": "a", "iat": int(time.time()), "exp": int(time.time()) + 60}).encode())
        sig = b64(hmac.new(json.dumps(JWK).encode(), f"{head}.{pay}".encode(), hashlib.sha256).digest())
        self.reject(f"{head}.{pay}.{sig}")
        with self.assertRaises(ValueError):
            JwtVerifier(issuer=ISS, audience=AUD, jwks=StaticJwks([JWK]), algorithms=("HS256",))

    def test_unknown_kid_garbage_and_overlong_lifetime_rejected(self):
        self.reject(token(kid="nope"))
        self.reject("not.a.jwt")
        now = int(time.time())
        self.reject(token(iat=now, exp=now + 86400))


class JwksTests(unittest.TestCase):
    def test_cached_throttled_and_stale_if_error(self):
        now = [0.0]
        calls = {"n": 0, "down": False}

        def handler(request):
            calls["n"] += 1
            if calls["down"]:
                return httpx.Response(503)
            return httpx.Response(200, json={"keys": [JWK]})

        async def go():
            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            jwks = HttpJwks("https://idp/jwks", client, ttl_s=100, min_refresh_interval_s=30, stale_if_error_s=1000, clock=lambda: now[0])
            self.assertIsNotNone(await jwks.get_key("k1"))
            await jwks.get_key("k1")
            self.assertEqual(calls["n"], 1)                       # cached
            now[0] = 40
            for _ in range(5):
                self.assertIsNone(await jwks.get_key("forged"))   # unknown kid: refresh is throttled
            self.assertEqual(calls["n"], 2)
            now[0] = 500; calls["down"] = True                     # ttl passed, IdP down: stale keys still serve
            self.assertIsNotNone(await jwks.get_key("k1"))
            now[0] = 5000                                          # too stale
            with self.assertRaises(AuthUnavailable):
                await jwks.get_key("k1")
        run(go())

    def test_never_fetched_and_down_is_unavailable_not_invalid(self):
        async def go():
            client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
            with self.assertRaises(AuthUnavailable):
                await HttpJwks("https://idp/jwks", client).get_key("k1")
        run(go())


class PolicyTests(unittest.TestCase):
    def p(self, role, tenant=TENANT_A):
        return Principal(tenant, "u", "s", role)

    def test_deny_by_default_and_unknown_role_or_action(self):
        self.assertFalse(authorize(self.p("student"), "document.write", facts=Facts(course_instructor=True)))
        self.assertFalse(authorize(self.p("root"), "chat.ask", facts=Facts(enrolled=True)))
        self.assertFalse(authorize(self.p("tenant_admin"), "does.not.exist"))

    def test_scope_is_required_on_top_of_role(self):
        self.assertTrue(authorize(self.p("student"), "chat.ask", facts=Facts(enrolled=True)))
        self.assertFalse(authorize(self.p("student"), "chat.ask", facts=Facts(enrolled=False)))
        self.assertTrue(authorize(self.p("instructor"), "document.write", facts=Facts(course_instructor=True)))
        self.assertFalse(authorize(self.p("instructor"), "document.write", facts=Facts()))  # another instructor's course
        self.assertTrue(authorize(self.p("tutor"), "ticket.open", facts=Facts(ticket_assigned_to_me=True)))
        self.assertFalse(authorize(self.p("tutor"), "ticket.open", facts=Facts()))         # unassigned ticket
        self.assertFalse(authorize(self.p("tutor"), "document.write", facts=Facts(course_instructor=True)))

    def test_admin_and_tenant_boundary(self):
        self.assertTrue(authorize(self.p("tenant_admin"), "admin.manage"))
        self.assertTrue(authorize(self.p("tenant_admin"), "audit.view"))
        self.assertFalse(authorize(self.p("instructor"), "audit.view"))
        self.assertFalse(authorize(self.p("tenant_admin"), "admin.manage", resource_tenant_id=TENANT_B))
        self.assertFalse(authorize(self.p("tenant_admin"), "chat.ask"))  # admins do not chat unless given another role


class RoleMappingTests(unittest.TestCase):
    def test_least_privilege_and_never_admin(self):
        self.assertEqual(initial_role([]), "student")
        self.assertEqual(initial_role(["instructor", "student"]), "instructor")
        self.assertEqual(initial_role(["tenant_admin"]), "student")
        self.assertEqual(initial_role(["sb-teachers"], {"sb-teachers": "instructor"}), "instructor")
        self.assertEqual(initial_role([None, 5, "x"]), "student")


class DirectoryMapTests(unittest.TestCase):
    """Entra: the tenant comes from the server-side map of directory id (tid) to tenant, never from a token claim."""
    TID = "11111111-2222-3333-4444-555555555555"

    def setUp(self):
        self.directory = InMemoryUserDirectory({TENANT_A, TENANT_B})
        self.seen = []
        self.rejects = []
        auth = BearerAuth(verifier(), self.directory, tenant_map={self.TID.upper(): TENANT_A}, on_reject=self.rejects.append)

        async def enrolled(identity, course_id):
            self.seen.append(identity)
            return True
        self.client = TestClient(create_app(make_orch(), authenticate=auth, check_enrollment=enrolled))

    def post(self, tok):
        return self.client.post("/chat", json=body(), headers={"authorization": f"Bearer {tok}"})

    def test_mapped_directory_works_without_a_tenant_claim_and_ignores_a_forged_one(self):
        self.assertEqual(self.post(token(tenant_id=None, tid=self.TID)).status_code, 200)
        self.assertEqual(self.seen[-1].tenant_id, TENANT_A)
        self.assertEqual(self.post(token(tenant_id=TENANT_B, tid=self.TID.upper())).status_code, 200)
        self.assertEqual(self.seen[-1].tenant_id, TENANT_A)            # the claim naming tenant B is ignored

    def test_unmapped_missing_or_odd_tid_is_rejected(self):
        for tid in ("99999999-2222-3333-4444-555555555555", None, 5, ["x"]):
            r = self.post(token(tenant_id=TENANT_A, tid=tid))
            self.assertEqual(r.status_code, 401, tid)
        self.assertEqual(self.seen, [])


class BearerApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = InMemoryUserDirectory({TENANT_A, TENANT_B})
        self.denylist = InMemoryDenylist()
        self.rejects = []
        auth = BearerAuth(verifier(), self.directory, self.denylist, on_reject=self.rejects.append)
        self.enroll_calls = []

        async def enrolled(identity, course_id):
            self.enroll_calls.append(identity)
            return course_id == "c1"

        self.client = TestClient(create_app(make_orch(), authenticate=auth, check_enrollment=enrolled))

    def post(self, tok=None, course="c1", **headers):
        h = dict(headers)
        if tok is not None:
            h["authorization"] = f"Bearer {tok}"
        return self.client.post("/chat", json=body(course=course), headers=h)

    def test_valid_token_streams_and_identity_comes_from_the_token(self):
        r = self.post(token(tenant_id=TENANT_A))
        self.assertEqual(r.status_code, 200)
        who = self.enroll_calls[-1]
        self.assertEqual(who.tenant_id, TENANT_A)
        self.assertEqual(who.role, "student")

    def test_client_supplied_tenant_headers_and_body_are_ignored(self):
        self.post(token(tenant_id=TENANT_A), **{"x-tenant-id": TENANT_B, "x-user-role": "tenant_admin"})
        who = self.enroll_calls[-1]
        self.assertEqual((who.tenant_id, who.role), (TENANT_A, "student"))
        r = self.client.post("/chat", json={**body(), "tenant_id": TENANT_B}, headers={"authorization": f"Bearer {token()}"})
        self.assertIn(r.status_code, (200, 422))
        if r.status_code == 200:
            self.assertEqual(self.enroll_calls[-1].tenant_id, TENANT_A)

    def test_failures_are_one_generic_401(self):
        bad = [None, "garbage", token(key=OTHER_KEY), token(aud="x"), token(tenant_id=None), token(tenant_id="not-a-uuid"),
               token(tenant_id=str(uuid.uuid4())), token(roles="admin")]
        bodies = set()
        for tok in bad:
            r = self.post(tok)
            self.assertEqual(r.status_code, 401, tok)
            self.assertEqual(r.headers["www-authenticate"], "Bearer")
            bodies.add(r.text)
        self.assertEqual(len(bodies), 1)
        self.assertEqual(self.enroll_calls, [])
        self.assertGreaterEqual(len(self.rejects), len(bad))

    def test_token_role_claims_cannot_make_an_admin_or_change_an_existing_role(self):
        self.assertEqual(self.post(token(roles=["tenant_admin"], sub="mallory")).status_code, 200)
        self.assertEqual(self.enroll_calls[-1].role, "student")          # the claim did not make an administrator
        self.post(token(roles=["instructor"], sub="mallory"))
        self.assertEqual(self.enroll_calls[-1].role, "student")          # and cannot promote an existing user


    def test_unenrolled_course_is_403_and_role_without_chat_is_403(self):
        self.assertEqual(self.post(token(), course="c2").status_code, 403)
        from studybuddy.auth import UserRecord
        self.directory.users[(TENANT_A, "boss")] = UserRecord("u-boss", "tenant_admin", "active")
        before = len(self.enroll_calls)
        self.assertEqual(self.post(token(sub="boss")).status_code, 403)
        self.assertEqual(len(self.enroll_calls), before)  # refused on role before any scope lookup

    def test_disabled_user_and_denylist(self):
        from studybuddy.auth import UserRecord
        self.directory.users[(TENANT_A, "gone")] = UserRecord("u-gone", "student", "disabled")
        self.assertEqual(self.post(token(sub="gone")).status_code, 401)
        tok = token(sub="bob")
        self.assertEqual(self.post(tok).status_code, 200)
        self.denylist.deny_subject(TENANT_A, "bob")
        self.assertEqual(self.post(tok).status_code, 401)
        tok2 = token(sub="carol", jti="j-1")
        self.denylist.deny_token("j-1")
        self.assertEqual(self.post(tok2).status_code, 401)

    def test_same_subject_in_two_tenants_are_different_users(self):
        self.post(token(sub="same", tenant_id=TENANT_A))
        self.post(token(sub="same", tenant_id=TENANT_B))
        self.assertNotEqual(self.directory.users[(TENANT_A, "same")].user_id, self.directory.users[(TENANT_B, "same")].user_id)

    def test_infrastructure_failures_are_503_not_401(self):
        class Down:
            async def is_denied(self, *a):
                raise RuntimeError("redis down")

        class Boom:
            async def resolve(self, *a):
                raise AuthUnavailable("db down")

        async def enrolled(identity, course_id):
            return True

        for auth in (BearerAuth(verifier(), self.directory, Down()), BearerAuth(verifier(), Boom())):
            c = TestClient(create_app(make_orch(), authenticate=auth, check_enrollment=enrolled))
            r = c.post("/chat", json=body(), headers={"authorization": f"Bearer {token()}"})
            self.assertEqual(r.status_code, 503)

        jwks_down = HttpJwks("https://idp/jwks", httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))))
        c = TestClient(create_app(make_orch(), authenticate=BearerAuth(verifier(jwks_down), self.directory), check_enrollment=enrolled))
        self.assertEqual(c.post("/chat", json=body(), headers={"authorization": f"Bearer {token()}"}).status_code, 503)


if __name__ == "__main__":
    unittest.main()
