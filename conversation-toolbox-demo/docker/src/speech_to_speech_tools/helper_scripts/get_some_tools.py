import json
from os.path import exists

auth_tok = "/etc/secrets/ezua/.auth_token"
placeholder = "FAKE_TOKEN"


def refresh_bearer_token():

    if exists(auth_tok):
        with open(auth_tok) as fid:
            return fid.read()
    return placeholder


bearer = refresh_bearer_token()
bearer_str = f"Bearer {bearer}"

tools = {
    "sql_mcp": {
        "url": "http://mcp-ezpresto-server.mcp-ezpresto-server.svc.cluster.local:9097/mcp",
        "headers": {"Authorization": bearer_str},
        "transport": "streamable-http",
    },
    "k8s_opts": {
        "url": "http://k8s-mcp-svc.project-user-francesco-caliva.svc.cluster.local:9090/mcp",
        "transport": "streamable-http",
    },
    "ddgs_mcp": {
        "url": "http://ddgs-lite-service.ddgs-mcp.svc.cluster.local:9090/mcp",
        "transport": "streamable-http",
    },
    "rag_mcp": {
        "url": "http://rag-mcp-server-mcp.mm-rag-mcp.svc.cluster.local:9090/mcp",
        "transport": "streamable-http",
    },
}

print(json.dumps(tools))
if bearer != placeholder:
    print("\n")
    print(bearer)
