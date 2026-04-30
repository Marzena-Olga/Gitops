import requests
import base64
import yaml
import re
import os
import urllib3
import urllib.parse

# =========================
# CONFIG
# =========================

PAT =  os.getenv("GITLAB_PAT")
if not PAT:
    raise RuntimeError("Missing GITLAB_PAT")

GITLAB_RO = os.getenv("GITLAB_RO")
if not GITLAB_RO:
    raise RuntimeError("Missing GITLAB_RO")

GITLAB_RO_TOKEN = os.getenv("GITLAB_RO_TOKEN")
if not GITLAB_RO_TOKEN:
    raise RuntimeError("Missing GITLAB_RO_TOKEN")

ARGOCD_TOKEN = os.getenv("ARGOCD_TOKEN")
if not ARGOCD_TOKEN:
    raise RuntimeError("Missing ARGOCD_TOKEN")

CI_PROJECT_NAMESPACE = os.environ["CI_PROJECT_NAMESPACE"]
if not CI_PROJECT_NAMESPACE:
    raise RuntimeError("Missing CI_PROJECT_NAMESPACE")

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

HEADERS = {"PRIVATE-TOKEN": PAT}

encoded_group = urllib.parse.quote(CI_PROJECT_NAMESPACE, safe="")
GROUP_URL = f"https://gitlab.nam.cz/api/v4/groups/{encoded_group}/projects"

VALUES_FILE_PATH = "chart/values.yaml"
APPLICATION_NAME = "nam-1bp-applicationset"

NAMESPACE = "argocd"

APP_NAMESPACE = "app-template"


# =========================
# UTIL
# =========================

def sanitize(name: str) -> str:
    """Kubernetes-safe namespace"""
    name = name.lower()
    name = re.sub(r'[^a-z0-9-]', '-', name)
    name = re.sub(r'-+', '-', name)
    return name[:63].strip('-')


# =========================
# GITLAB API
# =========================

def get_projects():
    """Fetch all projects from GitLab group"""
    projects = []
    page = 1

    while True:
        resp = requests.get(
            GROUP_URL,
            headers=HEADERS,
            params={"per_page": 100, "page": page}
        )

        data = resp.json()
        if not data:
            break

        projects.extend(data)
        page += 1

    return projects


def get_deployment_projects(projects):
    """Filter only *-deployment repos"""
    return [p for p in projects if p["name"].endswith("-deployment")]


# =========================
# HELM VALUES PARSER
# =========================

def extract_app_data(project):
    """
    Reads values.yaml from repo and extracts:
    - image repo
    - tag
    - app name
    """

    project_path = project["path_with_namespace"]
    encoded_project = project_path.replace("/", "%2F")
    file_path = VALUES_FILE_PATH.replace("/", "%2F")

    url = f"https://gitlab.nam.cz/api/v4/projects/{encoded_project}/repository/files/{file_path}"

    r = requests.get(
        url,
        headers=HEADERS,
        params={"ref": "main"}
    )

    if r.status_code != 200:
        print(f"[WARN] Missing values.yaml: {project['name']}")
        return None

    try:
        content = base64.b64decode(r.json()["content"]).decode()
        data = yaml.safe_load(content)

        deployment_enabled = data.get("deployment", {}).get("enabled") is True

        if not deployment_enabled:
            print(f"[SKIP] {project['name']} → deployment=false")
            return None

        app_name = data.get("fullnameOverride") or project["name"].replace("-deployment", "")
        app_name = sanitize(app_name)

        image_repo = data.get("image", {}).get("repository")
        image_tag = data.get("image", {}).get("tag")

        return {
            "name": app_name,
            "namespace": APP_NAMESPACE,
            "git": project["http_url_to_repo"],
            "url": project["web_url"],
            "image_repo": image_repo,
            "image_tag": image_tag
        }

    except Exception as e:
        print(f"[ERROR] YAML parse failed for {project['name']}: {e}")
        return None


def build_apps(projects):
    """Convert GitLab projects → structured app list"""
    apps = []

    for p in projects:
        app = extract_app_data(p)
        if app:
            apps.append(app)

    return apps


# =========================
# ARGOCD APPLICATIONSET
# =========================

def generate_applicationset(apps):
    """Build ArgoCD ApplicationSet manifest"""

    return {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "ApplicationSet",
        "metadata": {
            "name": APPLICATION_NAME,
            "namespace": NAMESPACE

        },
        "spec": {
            "generators": [
                {
                    "list": {
                        "elements": [
                            {
                                "name": a["name"],
                                "repoURL": a["git"],
                                "imageRepo": a["image_repo"],
                                "namespace": a["namespace"]
                            }
                            for a in apps
                        ]
                    }
                }
            ],
            "template": {
                "metadata": {
                    "name": "{{name}}",
                    "annotations": {
                        "argocd-image-updater.argoproj.io/image-list": "app={{imageRepo}}",
                        "argocd-image-updater.argoproj.io/app.update-strategy": "latest",
                        "argocd-image-updater.argoproj.io/app.allow-tags": "regexp:.*",
                        "argocd-image-updater.argoproj.io/app.helm.image-name": "image.repository",
                        "argocd-image-updater.argoproj.io/app.helm.image-tag": "image.tag"
                    }
                },
                "spec": {
                    "project": "default",
                    "source": {
                        "repoURL": "{{repoURL}}",
                        "targetRevision": "main",
                        "path": "./chart",
                        "helm": {
                            "valueFiles": ["values.yaml"]
                        }    
                    },
                    "destination": {
                        "server": "https://kubernetes.default.svc",
                        "namespace": "{{namespace}}"
                    },
                    "syncPolicy": {
                        "automated": {
                            "prune": True,
                            "selfHeal": True,
                            "enabled": True
                        },
                        "syncOptions": [
                            "CreateNamespace=true",
                            "ApplyOutOfSyncOnly=true"
                        ]
                    }
                }
            }
        }
    }

def register_repos(apps):

    url = "https://argocd.devk8s.nam.cz/api/v1/repositories?upsert=true"
    headers = {
        "Authorization": f"Bearer {ARGOCD_TOKEN}",
        "Content-Type": "application/json"
    }

    for a in apps:
        data = {
            "repo": a['git'],
            "type": "git",
            "name": a['name'],
            "username": GITLAB_RO,
            "password": GITLAB_RO_TOKEN
        }
        #print(data)
        response = requests.post(url, json=data, headers=headers, verify=False)
        print(a['name'], response.status_code, response.text)


# =========================
# MAIN PIPELINE
# =========================

if __name__ == "__main__":
    print("Fetching projects...")
    projects = get_projects()

    print("Filtering deployment repos...")
    deployment_projects = get_deployment_projects(projects)

    print(f"Found {len(deployment_projects)} deployment projects")

    print("Parsing values.yaml...")
    apps = build_apps(deployment_projects)

    print(f"Valid apps: {len(apps)}")

    print("Generating ApplicationSet...")
    appset = generate_applicationset(apps)
    
    #appset["metadata"]["annotations"] = {"generated-at": datetime.utcnow().isoformat()}
    print(appset)

    with open("applicationset.yaml", "w") as f:
        yaml.dump(appset, f, sort_keys=False)
    print("ApplicationSet seved to  applicationset.yaml")

    print("Registering repos in ArgoCD...")
    register_repos(apps)
