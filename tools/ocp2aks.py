#!/usr/bin/env python3
"""
OpenShift → AKS manifest transformer.

Transforms:
- DeploymentConfig → Deployment (apps/v1)
- Route           → Ingress (networking.k8s.io/v1)
- BuildConfig     → Skipped with an actionable note (builds move to CI/CD)

Usage (CLI example):
    python tools/ocp2aks/ocp2aks.py \
      --src openshift \
      --out dist/aks \
      --default-domain apps.example.com \
      --ingress-class nginx \
      --tls-secret myapp-tls \
      --image-registry myacr.azurecr.io \
      --repo-prefix apps

All options also read from environment variables:
  SRC_DIR, OUT_DIR, DEFAULT_DOMAIN, INGRESS_CLASS, TLS_SECRET,
  IMAGE_REGISTRY, REPO_PREFIX, REGISTRY_FALLBACK
"""

import argparse
import os
import pathlib
from copy import deepcopy
import yaml


# ----------------------------
# CLI / Environment parameters
# ----------------------------
def parse_args():
    p = argparse.ArgumentParser(description="OpenShift → AKS transformer")
    p.add_argument("--src", dest="src_dir",
                   default=os.getenv("SRC_DIR", "openshift"),
                   help="Directory containing OpenShift YAML")
    p.add_argument("--out", dest="out_dir",
                   default=os.getenv("OUT_DIR", "./output"),
                   help="Output directory for AKS-native manifests")
    p.add_argument("--default-domain", dest="default_domain",
                   default=os.getenv("DEFAULT_DOMAIN", "apps.example.com"),
                   help="Default domain for Routes that do not specify a host")
    p.add_argument("--ingress-class", dest="ingress_class",
                   default=os.getenv("INGRESS_CLASS", "nginx"),
                   help="Ingress class (e.g., nginx, azure/application-gateway)")
    p.add_argument("--tls-secret", dest="tls_secret",
                   default=os.getenv("TLS_SECRET", ""),
                   help="TLS secret name to use in Ingress (optional)")
    p.add_argument("--image-registry", dest="image_registry",
                   default=os.getenv("IMAGE_REGISTRY", os.getenv("REGISTRY_FALLBACK", "")),
                   help="Registry override for container images (e.g., myacr.azurecr.io)")
    p.add_argument("--repo-prefix", dest="repo_prefix",
                   default=os.getenv("REPO_PREFIX", ""),
                   help="Optional repository prefix in target registry (e.g., apps)")
    return p.parse_args()


# ----------------------------
# Helpers
# ----------------------------
def is_yaml_file(p: pathlib.Path) -> bool:
    return p.suffix.lower() in (".yml", ".yaml")


def norm_labels(meta: dict) -> dict:
    """Ensure labels key exists."""
    meta = meta or {}
    meta.setdefault("labels", {})
    return meta


def map_image(image: str, name_hint: str, image_registry: str, repo_prefix: str) -> str:
    """
    Map OpenShift ImageStream-like references to an ACR (or target registry) reference if requested.

    Rules:
    - If 'image' already looks fully-qualified (contains registry/repo:tag), keep as-is.
    - If image_registry already contains a tag (has ':'), use it as-is (it's a complete image reference).
    - If empty or imagestream-ish, map to:
         IMAGE_REGISTRY[/REPO_PREFIX]/<name_hint>:latest
      (Tag pinning is expected in CD; ':latest' is a safe placeholder here.)
    """
    image = image or ""
    # Simple heuristic to detect fully-qualified references
    if image and ("/" in image or "." in image) and ":" in image:
        return image

    # If IMAGE_REGISTRY already contains a tag, use it as-is (it's a complete image reference)
    if image_registry and ":" in image_registry:
        return image_registry

    repo = name_hint
    if repo_prefix:
        repo = f"{repo_prefix}/{repo}"

    if image_registry:
        return f"{image_registry}/{repo}:latest"
    return image or f"{repo}:latest"


# ----------------------------
# Converters
# ----------------------------
def to_deployment(dc: dict, image_registry: str, repo_prefix: str) -> dict:
    """Convert OpenShift DeploymentConfig → Kubernetes Deployment (apps/v1)."""
    meta = deepcopy(dc.get("metadata", {}))
    spec = deepcopy(dc.get("spec", {}))
    name = meta.get("name", "app")
    meta = norm_labels(meta)

    template = spec.get("template", {}) or {}
    tpl_meta = norm_labels(template.get("metadata", {}) or {})
    tpl_spec = template.get("spec", {}) or {}
    
    # Get labels and deep copy to avoid YAML anchors
    labels = deepcopy(tpl_meta.get("labels") or meta.get("labels") or {"app": name})
    selector_labels = deepcopy(labels)
    template_labels = deepcopy(labels)
    
    print(f"template meta 111: {labels} ")

    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": name,
            "labels": labels,
            "annotations": {
                k: v for k, v in (meta.get("annotations") or {}).items()
                if not k.startswith("openshift.io/")
            },
        },
        "spec": {
            "replicas": spec.get("replicas", 1),
            "selector": {"matchLabels": selector_labels},
            "template": {
                "metadata": {
                    "labels": template_labels,
                    "annotations": {
                        k: v for k, v in (tpl_meta.get("annotations") or {}).items()
                        if not k.startswith("openshift.io/")
                    },
                },
                "spec": tpl_spec,
            },
            "strategy": {},
        },
    }

    # Strategy (RollingUpdate/Recreate)
    st = spec.get("strategy", {}) or {}
    st_type = (st.get("type") or "Rolling").lower()
    if st_type.startswith("recreate"):
        deployment["spec"]["strategy"] = {"type": "Recreate"}
    else:
        params = (st.get("rollingParams") or {})
        deployment["spec"]["strategy"] = {
            "type": "RollingUpdate",
            "rollingUpdate": {
                "maxSurge": params.get("maxSurge", "25%"),
                "maxUnavailable": params.get("maxUnavailable", "25%"),
            },
        }

    # Remove OpenShift-only fields
    for fld in ("triggers", "test", "paused"):
        deployment["spec"].pop(fld, None)

    # Normalize container images + default pull policy
    containers = deployment["spec"]["template"]["spec"].get("containers", [])
    for c in containers:
        img = c.get("image", "")
        c["image"] = map_image(
            img,
            name_hint=c.get("name", name),
            image_registry=image_registry,
            repo_prefix=repo_prefix,
        )
        if not c.get("imagePullPolicy"):
            c["imagePullPolicy"] = "IfNotPresent"

    return deployment


def to_ingress(route: dict, default_domain: str, ingress_class: str, tls_secret: str) -> dict:
    """Convert OpenShift Route → Kubernetes Ingress (networking.k8s.io/v1)."""
    meta = deepcopy(route.get("metadata", {}))
    spec = deepcopy(route.get("spec", {}) or {})
    name = meta.get("name", "web")

    host = spec.get("host") or f"{name}.{default_domain}"
    to_ref = (spec.get("to") or {})
    svc_name = to_ref.get("name") or name
    target_port = ((spec.get("port") or {}).get("targetPort")) or 80  # can be int or str

    ingress = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": {
            "name": name,
            "labels": {k: v for k, v in (meta.get("labels") or {}).items()},
            # If you prefer spec.ingressClassName, you can move this to spec.ingressClassName below
            "annotations": {
                "kubernetes.io/ingress.class": ingress_class
            },
        },
        "spec": {
            "rules": [{
                "host": host,
                "http": {
                    "paths": [{
                        "path": "/",
                        "pathType": "Prefix",
                        "backend": {
                            "service": {
                                "name": svc_name,
                                "port": (
                                    {"name": target_port} if isinstance(target_port, str)
                                    else {"number": int(target_port)}
                                )
                            }
                        }
                    }]
                }
            }]
        }
    }

    # TLS: prefer explicit input, else assume a same-named secret if Route had TLS
    tls_entries = []
    if tls_secret:
        tls_entries.append({"hosts": [host], "secretName": tls_secret})
    elif spec.get("tls"):
        tls_entries.append({"hosts": [host], "secretName": f"{name}-tls"})

    if tls_entries:
        ingress["spec"]["tls"] = tls_entries

    return ingress


# ----------------------------
# File naming helper
# ----------------------------
def map_output_filename(source_path, out_docs):
    """Map OpenShift filenames to AKS equivalents based on converted resource kinds."""
    name = source_path.name
    
    # Collect kinds in the output documents
    kinds_in_output = {doc.get("kind") for doc in out_docs if isinstance(doc, dict)}
    
    # Map OpenShift resource names to AKS equivalents
    mappings = {
        ("DeploymentConfig", "deploymentconfig"): "deployment.yaml",
        ("Route", "route"): "ingress.yaml",
    }
    
    # Check if file name or kinds match, apply mapping
    name_lower = name.lower()
    for (kind, pattern), new_name in mappings.items():
        if pattern in name_lower or kind in kinds_in_output:
            return source_path.with_name(new_name)
    
    # Default: keep original name
    return source_path


# ----------------------------
# Main transform loop
# ----------------------------
def main():
    args = parse_args()
    src = pathlib.Path(args.src_dir)
    out = pathlib.Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    #print(f"source arg: {src}" )

    report_path = out.parent / "transform-report.md"
    summary = {"DeploymentConfig": 0, "Route": 0, "BuildConfig": 0, "Other": 0}
    converted = 0
    warnings = []

    for path in src.rglob("*"):
        if not path.is_file() or not is_yaml_file(path):
            continue

        try:
            docs = list(yaml.safe_load_all(path.read_text()))
        except Exception as e:
            warnings.append(f"Failed to parse {path}: {e}")
            continue

        out_docs = []
        for doc in docs:
            if not isinstance(doc, dict) or "kind" not in doc:
                continue

            kind = doc.get("kind")
            if kind == "DeploymentConfig":
                summary["DeploymentConfig"] += 1
                out_docs.append(to_deployment(doc, args.image_registry, args.repo_prefix))
                converted += 1

            elif kind == "Route":
                summary["Route"] += 1
                out_docs.append(to_ingress(doc, args.default_domain, args.ingress_class, args.tls_secret))
                converted += 1

            elif kind == "BuildConfig":
                summary["BuildConfig"] += 1
                warnings.append(
                    f"BuildConfig '{doc.get('metadata', {}).get('name', 'unnamed')}' skipped: "
                    f"move builds to CI (e.g., GitHub Actions) and push images to "
                    f"{args.image_registry or 'ACR'}."
                )

            else:
                # Pass-through for k8s-native resources; remove OpenShift-only annotations
                summary["Other"] += 1
                if isinstance(doc.get("metadata"), dict):
                    anns = doc["metadata"].get("annotations") or {}
                    anns = {k: v for k, v in anns.items() if not k.startswith("openshift.io/")}
                    if anns:
                        doc["metadata"]["annotations"] = anns
                    else:
                        doc["metadata"].pop("annotations", None)
                out_docs.append(doc)

        #print(f"Output {len(out_docs)}")
        if out_docs:
            mapped_filename = map_output_filename(path, out_docs)
            dest = (out / mapped_filename.name).with_suffix(".yaml")
            print(f"doc(s) to: {dest}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "w") as g:
                for i, d in enumerate(out_docs):
                    yaml.safe_dump(d, g, sort_keys=False)
                    if i < len(out_docs) - 1:
                        g.write("---\n")

    # Report (what happened + things to review)
    lines = []
    lines.append("# OpenShift → AKS Transformation Report\n")
    lines.append(f"- Source dir: `{args.src_dir}`")
    lines.append(f"- Output dir: `{args.out_dir}`")
    lines.append(f"- Ingress class: `{args.ingress_class}`")
    if args.tls_secret:
        lines.append(f"- TLS secret: `{args.tls_secret}`")
    if args.image_registry:
        lines.append(f"- Image registry override: `{args.image_registry}`")
    if args.repo_prefix:
        lines.append(f"- Repo prefix: `{args.repo_prefix}`")

    lines.append("\n## Summary\n")
    lines.append(f"- DeploymentConfigs converted: **{summary['DeploymentConfig']}**")
    lines.append(f"- Routes converted: **{summary['Route']}**")
    lines.append(f"- BuildConfigs skipped (see notes): **{summary['BuildConfig']}**")
    lines.append(f"- Other resources passed through: **{summary['Other']}**")
    lines.append(f"- Total converted: **{converted}**\n")

    if warnings:
        lines.append("## Warnings / Notes\n")
        for w in warnings:
            lines.append(f"- {w}")
        lines.append("")

    # Ensure parent directory exists
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        print(f"Writing report to: {report_path}")
        report_path.write_text("\n".join(lines), encoding='utf-8')
    except Exception as e:
        print(f"Failed to write report: {e}")

    


if __name__ == "__main__":
    main()