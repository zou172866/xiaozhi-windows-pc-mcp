# Anima Brain documentation

Project overview: [English](../README.md) · [简体中文](../README.zh-CN.md).
The detailed guides below are currently in English.

Anima Brain is the public project name. Existing Python imports (`companion_engine`),
the `companion` CLI, and host/plugin identifiers retain their original names for compatibility.

Start with the [project README](../README.md) for the Python SDK quickstart. This index
helps you choose the next guide without reading the whole repository.

## Build an application

1. [SDK guide](sdk.md): create a character, send a message, inspect memory, handle review
   and recover an interrupted turn. Both async and synchronous calls are covered.
2. [SDK capabilities](sdk-capabilities.md): configure providers, history, memory budgets,
   image input, image generation, reference edits, speech and transcription.
3. [Memory automation](memory-automation.md): explicitly enable embedding and summary
   maintenance, inspect its state and understand when provider calls happen.

Runnable entry points:

| Example | Purpose |
| --- | --- |
| [`Companion Studio`](../examples/showcase/README.md) | Optional responsive web example: chat, memory editing and context inspection |
| [`examples/chat.py`](../examples/chat.py) | Real-model chat in the terminal using the SDK |
| [`examples/multimodal.py`](../examples/multimodal.py) | Explicit vision, image and voice operations |
| [`examples/offline_demo.py`](../examples/offline_demo.py) | Explore the engine without model credentials |

## Connect an existing agent

- [Host integrations](host-integrations.md): generate and install the Claude / DeepSeek
  Harness setup, then verify that the host actually calls the tools.
- [Connection modes](connections.md): stdio, loopback HTTP and the ChatGPT connection
  boundary. These are distinct from the SDK's automatic `chat()` workflow.
- [Feature reference](features.md): CLI, provider configuration and memory/media tools.

## Understand the design

- [Architecture](architecture.md): turn lifecycle, evidence, storage, retrieval and limits.
- [Brain adaptation](brain-adaptation.md): historical implementation notes for the 0.4
  adaptation. Read the SDK guides above for the current public interface.
- [Security](../SECURITY.md): local identity, provider data, secrets and hosting boundaries.

## Contribute and release

- [Contributing](../CONTRIBUTING.md): environment setup and development workflow.
- [Verification](verification.md): what is tested and what still needs live evaluation.
- [Releasing](releasing.md): source and package checks before distribution.
- [Changelog](../CHANGELOG.md): version history.
- [Visual assets](assets/README.md): sources and usage terms for the public Anima imagery.

The SDK is currently alpha. It stores state locally; it does not supply a hosted backend,
model weights or model-provider credentials.

- [Interactive Brain Atlas](brain-flow.md): semantic/episodic separation, retrieval formulas, consolidation cycles and emotional continuity, with four animated views and formula explorers.
- [Live voice and video](live-calls.md): provider setup, SDK streaming API, persistence, cleanup and hosting boundaries.

- [English demo clips](demos.md): voice, audio calls and animated video, with playback links and recording notes.
