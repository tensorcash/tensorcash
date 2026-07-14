#!/usr/bin/env python3
"""Node-side onion claim + refresh agent.

Replaces per-node self-grinding for nodes that CONSUME from the central
onion-grinder pool. It:

  1. On startup (no valid onion) or when the installed onion's freshness tag is
     about to age out of the window, CLAIMS a fresh bundle from the pool
     (k8s Secrets in the onion-grinder namespace) via an atomic compare-and-set
     on resourceVersion -> one-onion-one-node, no two nodes take the same bundle.
  2. INSTALLS the claimed 96-byte key via SIGHUP (never a Tor restart -> no cold
     bootstrap), sets externalip, and restarts bitcoind only when quiescent.
  3. REFRESHES *before* expiry: it rotates when (expiry_height - tip) drops below
     ROTATION_BUFFER, so the node's onion never goes stale and never falls to
     zero diversity credit (the failure net_processing.h:122 documents).

Prefix-agnostic: works identically for `ten` and `tensorc` — the node cannot
grind `tensorc` itself, which is the whole reason the pool exists.

Dependency-free: talks to the k8s API with the in-cluster ServiceAccount token
via urllib (no kubernetes client lib needed in the node image).
"""
import os, ssl, json, time, base64, shutil, signal, subprocess, logging, urllib.request, urllib.parse, urllib.error

LOG = logging.getLogger("onion-claim-agent")
logging.basicConfig(level=logging.INFO, format="%(asctime)sZ onion-claim-agent: %(message)s")

POOL_NS         = os.environ.get("POOL_NAMESPACE", "onion-grinder")
HS_DIR          = os.environ.get("HS_DIR", "/var/lib/tor/tensorcash-service")
DATA_DIR        = os.environ.get("DATA_DIR", "/data")
CONF            = os.environ.get("BITCOIN_CONF", os.path.join(DATA_DIR, "bitcoin.conf"))
P2P_PORT        = os.environ.get("P2P_PORT", "29241")
ROTATION_BUFFER = int(os.environ.get("ROTATION_BUFFER", "200"))   # refresh when expiry-tip < this
CHECK_INTERVAL  = int(os.environ.get("CHECK_INTERVAL", "120"))
WANT_PREFIX     = os.environ.get("VANITY_PREFIX", "")             # "" = accept any pool prefix
NODE_ID         = os.environ.get("HOSTNAME", "node")
EXPIRY_FILE     = os.path.join(HS_DIR, ".expiry_height")          # our record of current onion's expiry

SA   = "/var/run/secrets/kubernetes.io/serviceaccount"
APIH = os.environ.get("KUBERNETES_SERVICE_HOST"); APIP = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
API  = f"https://{APIH}:{APIP}"
# Production reads the in-pod ServiceAccount token; K8S_TOKEN/K8S_CA env overrides
# exist only so the agent can be exercised before the pod is redeployed with its
# dedicated SA. Identical code path either way.
TOKEN = os.environ.get("K8S_TOKEN") or open(os.path.join(SA, "token")).read().strip()
CTX   = ssl.create_default_context(cafile=os.environ.get("K8S_CA", os.path.join(SA, "ca.crt")))

# ---------------- k8s + chain helpers ----------------
def k8s(method, path, body=None):
    req = urllib.request.Request(API + path, data=(json.dumps(body).encode() if body is not None else None), method=method)
    req.add_header("Authorization", f"Bearer {TOKEN}"); req.add_header("Accept", "application/json")
    if body is not None: req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, context=CTX, timeout=20) as r:
        return r.status, json.loads(r.read() or "{}")

def bitcoin_cli(*args):
    r = subprocess.run(["bitcoin-cli", f"-datadir={DATA_DIR}", f"-conf={CONF}", *args],
                       capture_output=True, text=True, timeout=20)
    return r.returncode, r.stdout.strip(), r.stderr.strip()

def get_tip():
    rc, out, _ = bitcoin_cli("getblockcount")
    return int(out) if rc == 0 and out.isdigit() else None

def read_current_expiry():
    try:    return int(open(EXPIRY_FILE).read().strip())
    except Exception: return None

def list_fresh_bundles(tip):
    sel = "app.kubernetes.io/name=onion-grinder,onion-grinder/claimed=false"
    _, body = k8s("GET", f"/api/v1/namespaces/{POOL_NS}/secrets?labelSelector={urllib.parse.quote(sel)}")
    out = []
    for s in body.get("items", []):
        a = s.get("metadata", {}).get("annotations", {}) or {}
        lbl = s.get("metadata", {}).get("labels", {}) or {}
        exp = int(a.get("onion-grinder/expiry_height", "0"))
        if WANT_PREFIX and lbl.get("onion-grinder/prefix") != WANT_PREFIX:
            continue
        if exp - tip <= ROTATION_BUFFER:      # must give us MORE than a buffer of runway
            continue
        out.append((exp, s))
    out.sort(key=lambda t: t[0], reverse=True)  # most runway first
    return out

def claim(secret):
    """Atomic CAS: PUT with the same resourceVersion; 409 => lost the race."""
    md = secret["metadata"]
    md.setdefault("labels", {})["onion-grinder/claimed"] = "true"
    md.setdefault("annotations", {})["onion-grinder/claimed_by"] = NODE_ID
    try:
        st, _ = k8s("PUT", f"/api/v1/namespaces/{POOL_NS}/secrets/{md['name']}", body=secret)
        return st == 200
    except urllib.error.HTTPError as e:
        if e.code == 409:
            LOG.info("bundle %s claimed by another node (409) — trying next", md["name"])
            return False
        raise

# ---------------- install (mirrors the proven SIGHUP path) ----------------
def install(secret):
    body = base64.b64decode(secret["data"]["hs_ed25519_secret_key"])
    assert len(body) == 96 and body[:29] == b"== ed25519v1-secret: type0 ==", "bad key blob"
    # owner MUST match whoever runs Tor (prod=debian-tor); derive from current HS_DIR
    owner = None
    if os.path.isdir(HS_DIR):
        import pwd, grp, stat as _s
        st = os.stat(HS_DIR); owner = (pwd.getpwuid(st.st_uid).pw_name, grp.getgrgid(st.st_gid).gr_name)
    staging = HS_DIR + ".staging"
    shutil.rmtree(staging, ignore_errors=True); os.makedirs(staging, 0o700)
    with open(os.path.join(staging, "hs_ed25519_secret_key"), "wb") as f: f.write(body)
    os.chmod(os.path.join(staging, "hs_ed25519_secret_key"), 0o600)
    if owner:
        subprocess.run(["chown", "-R", f"{owner[0]}:{owner[1]}", staging], check=False)
    shutil.rmtree(HS_DIR, ignore_errors=True); os.rename(staging, HS_DIR)
    # SIGHUP Tor (NOT restart) so it reloads the new key in place
    if subprocess.run(["supervisorctl", "signal", "HUP", "tor"], capture_output=True).returncode != 0:
        pids = subprocess.run(["pidof", "tor"], capture_output=True, text=True).stdout.split()
        if pids: os.kill(int(pids[0]), signal.SIGHUP)
        else: LOG.error("could not signal Tor to reload")
    # wait for Tor to re-derive hostname
    onion = None
    for _ in range(15):
        try: onion = open(os.path.join(HS_DIR, "hostname")).read().strip()
        except Exception: onion = None
        if onion: break
        time.sleep(1)
    return onion

def set_externalip(onion):
    ext = f"externalip={onion}:{P2P_PORT}"
    lines, seen_e, seen_l = [], False, False
    for ln in open(CONF).read().splitlines():
        if ln.startswith("externalip="): ln, seen_e = ext, True
        if ln.startswith("listenonion="): ln, seen_l = "listenonion=0", True
        lines.append(ln)
    if not seen_e: lines.append(ext)
    if not seen_l: lines.append("listenonion=0")
    open(CONF, "w").write("\n".join(lines) + "\n")

def restart_bitcoind_if_quiescent():
    rc, out, _ = bitcoin_cli("getblockchaininfo")
    try: info = json.loads(out)
    except Exception: return False
    b, h = info.get("blocks", 0), info.get("headers", 0)
    if b > 0 and b == h:
        subprocess.run(["supervisorctl", "restart", "node"], capture_output=True)
        LOG.info("quiescent (blocks=%d==headers) — restarted bitcoind to apply externalip", b)
        return True
    LOG.info("NOT quiescent (blocks=%s headers=%s) — externalip persisted, applies on next clean restart", b, h)
    return False

# ---------------- decide + rotate ----------------
def need_refresh(tip):
    if not os.path.exists(os.path.join(HS_DIR, "hostname")):
        return True, "no onion installed"
    exp = read_current_expiry()
    if exp is None:
        return True, "unknown expiry (no marker)"
    if exp - tip <= ROTATION_BUFFER:
        return True, f"about to expire (expiry={exp} tip={tip} buffer={ROTATION_BUFFER})"
    return False, f"fresh (expiry={exp} tip={tip}, {exp-tip} blocks of runway)"

def rotate(tip):
    for exp, secret in list_fresh_bundles(tip):
        name = secret["metadata"]["name"]
        if not claim(secret):
            continue
        LOG.info("claimed %s (expiry_height=%d, %d blocks runway)", name, exp, exp - tip)
        onion = install(secret)
        if not onion:
            LOG.error("Tor did not derive hostname after installing %s", name); return False
        set_externalip(onion)
        with open(EXPIRY_FILE, "w") as f: f.write(str(exp))
        restart_bitcoind_if_quiescent()
        LOG.info("INSTALLED onion=%s expiry_height=%d (source pool bundle %s)", onion, exp, name)
        return True
    LOG.warning("no fresh pool bundle available to claim (pool empty or all near-expiry) — keeping current onion, will retry")
    return False

def ensure_externalip_advertised():
    """Re-assert externalip for the current onion if it is missing from the conf.

    The install-config initContainer regenerates bitcoin.conf from the ConfigMap
    on EVERY pod start, wiping the externalip= line set_externalip() wrote — and
    a fresh (non-rotating) onion never re-enters rotate(), so without this check
    a restarted node silently stops advertising its claimed onion forever.
    """
    hostname_file = os.path.join(HS_DIR, "hostname")
    if not os.path.exists(hostname_file):
        return
    onion = open(hostname_file).read().strip()
    if not onion:
        return
    want = f"externalip={onion}:{P2P_PORT}"
    try:
        conf = open(CONF).read()
    except OSError:
        return
    if any(ln.strip() == want for ln in conf.splitlines()):
        return
    LOG.info("externalip missing/stale in %s (initContainer conf regeneration) — re-asserting %s", CONF, want)
    set_externalip(onion)
    restart_bitcoind_if_quiescent()

def tick():
    tip = get_tip()
    if tip is None:
        LOG.info("bitcoind RPC not ready yet"); return
    refresh, why = need_refresh(tip)
    if refresh:
        LOG.info("refresh needed: %s", why); rotate(tip)
    else:
        LOG.info("no refresh: %s", why)
        ensure_externalip_advertised()

def main():
    LOG.info("up: pool_ns=%s hs_dir=%s buffer=%d interval=%ds prefix=%s",
             POOL_NS, HS_DIR, ROTATION_BUFFER, CHECK_INTERVAL, WANT_PREFIX or "<any>")
    once = os.environ.get("RUN_ONCE") == "1"
    while True:
        try:
            tick()
        except Exception as e:
            LOG.error("loop error: %s", e)
        if once:
            break
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
