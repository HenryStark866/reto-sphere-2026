---
title: Sara - Seguimiento Postoperatorio por Voz
emoji: 🩺
colorFrom: blue
colorTo: green
sdk: docker
app_port: 8080
pinned: false
license: mit
short_description: Agente de voz para seguimiento postoperatorio con RAG clínico
---

# Sara — agente de voz para seguimiento postoperatorio

Solución al **Tech Sphere Challenge 2026**. Un agente que llama por voz a un paciente
operado hace pocos días, conversa en español colombiano, fundamenta cada afirmación
clínica en un corpus de 107 documentos clínicos, y decide si el caso debe escalar a
personal capacitado.

| Superficie | Ruta |
|---|---|
| Interfaz de llamada | [`/llamada`](/llamada) |
| Consola de administración | [`/consola`](/consola) |
| Estado del sistema | [`/api/salud`](/api/salud) |
| API | [`/docs`](/docs) |

**Código fuente, arquitectura, informe y arnés de evaluación:**
<https://github.com/HenryStark866/reto-sphere-2026>

## Cómo probarlo

1. Abre **`/llamada`**, elige un paciente y pulsa *Iniciar llamada*. Necesitas permitir el
   micrófono; si no lo tienes, hay un campo de texto de respaldo que recorre el mismo
   camino salvo la transcripción.
2. Abre **`/consola`** para subir un documento clínico y ver cómo el agente empieza a
   citarlo, o eliminarlo y comprobar que deja de hacerlo.

## Limitaciones de esta demo alojada

- **El almacenamiento es efímero.** La base de llamadas y los documentos que subas viven
  en `/tmp` y se pierden cuando el Space se reinicia. En el levantamiento local la
  persistencia es real, en `data/llamadas.db`.
- **Una sola instancia.** El índice vivo y las llamadas en curso viven en la memoria del
  proceso, a propósito: es lo que permite que un documento subido esté disponible al
  instante siguiente.
- **El Space se duerme** tras un periodo largo sin visitas y tarda unos segundos en
  despertar.
- Los datos clínicos son **sintéticos** y no tienen validez clínica alguna.
