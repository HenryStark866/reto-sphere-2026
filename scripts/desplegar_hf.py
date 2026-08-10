"""Despliega el agente en un Space de Hugging Face. Sin costo y sin tarjeta.

Por que aqui y no en Cloud Run: Cloud Run tiene capa gratuita, pero exige una
cuenta de facturacion activa para poder habilitarse siquiera. Un Space publico
con CPU basica es gratuito de forma permanente, da 2 vCPU y 16 GB de RAM -de
sobra para el modelo ONNX y el indice en memoria- y entrega una URL HTTPS
publica.

Requisitos:
    - Una cuenta gratuita en huggingface.co (la crea una persona, no un script).
    - Un token con permiso de escritura: huggingface.co/settings/tokens
    - La llave de Groq, que se guarda como secreto del Space, nunca en el codigo.

Uso:
    python -m scripts.desplegar_hf --token hf_xxx --llave-groq gsk_xxx
    python -m scripts.desplegar_hf --token hf_xxx --espacio mi-usuario/sara-postop
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

# Lo que necesita la imagen para correr. Se sube esto y nada mas: el corpus de
# PDFs pesa cientos de megas y no hace falta, porque el indice ya esta hecho.
PATRONES = [
    "Dockerfile",
    "requirements.txt",
    "LICENSE",
    "app/**",
    "eval/**",
    "index/**",
    "scripts/build_index.py",
    "scripts/evaluar.py",
    "dataset/*.xlsx",
]
EXCLUIR = ["**/__pycache__/**", "**/*.pyc"]


def main() -> int:
    ap = argparse.ArgumentParser(description="Despliega el agente en Hugging Face Spaces")
    ap.add_argument("--token", required=True, help="token de escritura de Hugging Face")
    ap.add_argument("--espacio", default=None,
                    help="usuario/nombre del Space (por defecto <tu-usuario>/sara-postop)")
    ap.add_argument("--llave-groq", default="", help="GROQ_API_KEY, se guarda como secreto")
    ap.add_argument("--privado", action="store_true",
                    help="crea el Space privado (los Spaces privados con CPU basica tambien son gratuitos)")
    args = ap.parse_args()

    from huggingface_hub import HfApi

    api = HfApi(token=args.token)

    try:
        usuario = api.whoami()["name"]
    except Exception as exc:
        print(f"El token no sirve: {type(exc).__name__}: {exc}")
        return 1
    print(f"Autenticado como {usuario}")

    espacio = args.espacio or f"{usuario}/sara-postop"

    # --- comprobaciones antes de subir nada --------------------------------
    if not (BASE_DIR / "index" / "vectores.npy").exists():
        print("Falta el indice construido. Ejecuta: python -m scripts.build_index")
        return 1
    mb = sum(f.stat().st_size for f in (BASE_DIR / "index").iterdir()) / 1024 / 1024
    print(f"Indice presente: {mb:.1f} MB")

    portada = BASE_DIR / "deploy" / "espacio_README.md"
    if not portada.exists():
        print(f"Falta {portada}")
        return 1

    # --- crear el Space -----------------------------------------------------
    print(f"\nCreando o reutilizando el Space {espacio}...")
    api.create_repo(
        repo_id=espacio,
        repo_type="space",
        space_sdk="docker",
        private=args.privado,
        exist_ok=True,
    )

    # --- el secreto va antes que el codigo ---------------------------------
    # Si se sube el codigo primero, el Space arranca a construir sin la llave y
    # el primer despliegue queda sin voz hasta el siguiente reinicio.
    if args.llave_groq:
        print("Guardando GROQ_API_KEY como secreto del Space...")
        api.add_space_secret(
            repo_id=espacio,
            key="GROQ_API_KEY",
            value=args.llave_groq,
            description="Llave de Groq para el modelo de lenguaje y la transcripcion",
        )
    else:
        print("AVISO: sin --llave-groq el Space arranca, pero el agente no puede hablar.")
        print("       Se puede anadir despues en Settings > Variables and secrets.")

    # --- el README del Space lleva el front-matter que lo configura ---------
    print("Subiendo la portada del Space...")
    api.upload_file(
        path_or_fileobj=str(portada),
        path_in_repo="README.md",
        repo_id=espacio,
        repo_type="space",
        commit_message="Portada y configuracion del Space",
    )

    print("Subiendo la aplicacion y el indice...")
    api.upload_folder(
        folder_path=str(BASE_DIR),
        repo_id=espacio,
        repo_type="space",
        allow_patterns=PATRONES,
        ignore_patterns=EXCLUIR,
        commit_message="Agente de seguimiento postoperatorio",
    )

    url = f"https://huggingface.co/spaces/{espacio}"
    directa = f"https://{espacio.replace('/', '-').lower()}.hf.space"
    print(f"\nSubido. La construccion tarda varios minutos (instala dependencias y")
    print(f"descarga el modelo de embeddings dentro de la imagen).")
    print(f"\n  Registro de construccion : {url}?logs=build")
    print(f"  URL de la aplicacion     : {directa}")
    print(f"\n  Llamada : {directa}/llamada")
    print(f"  Consola : {directa}/consola")
    print(f"  Salud   : {directa}/api/salud")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
