# Extending DigLibrary

DigLibrary ships one acquisition provider (slskd) and three metadata sources.
Everything else is meant to be added from outside, without editing the core.

This document is the contract for that. If adding an extension requires a change
to any module listed under "What you never touch", that is a bug in DigLibrary,
not a limitation you should work around.

## What you never touch

- `diglibrary.engines` — Engine contracts
- `diglibrary.providers` — the provider framework itself
- `diglibrary.connectors.contracts` and `diglibrary.connectors.models`
- `diglibrary.metadata.service` — the Metadata Engine
- `diglibrary.config.loader` — configuration validation

None of these contains a list of known providers or known sources. Identity is
validated by format only (`diglibrary.identity`), never against an enumeration.

## Adding an acquisition provider

A provider answers "where can this release be obtained from", and returns
candidates. It never selects one — that is the Decision Engine's job.

1. Implement the `Provider` protocol in `diglibrary.providers.contracts`. It is
   structural: you inherit from nothing.
2. Declare a `ProviderMetadata` with your own `ProviderId`. Any lowercase
   snake-case identifier is valid; you do not register it anywhere in the core.
3. Declare your capabilities with `ProviderCapability`. The names in
   `ProviderCapabilities` are conveniences, not a closed set — a capability the
   project never imagined is a valid capability.
4. Talk to your service through a connector that implements
   `SearchConnector`, so your provider stays free of HTTP and protocol detail.
5. Register the instance in your own composition code:
   `registry.register(YourProvider(...))`.
6. Add your identifier to `[providers] enabled` and `priority` in `config.toml`,
   and your own settings under `[providers.settings.your_provider]`. Unknown
   keys there are preserved verbatim and handed to you.

Your provider's lifecycle is driven only by `ProviderManager`. Do not call
another provider, and do not instantiate your dependencies inside the provider —
receive them.

## Adding a metadata source

A metadata source answers "what release is this, authoritatively". Precedence
between sources is configuration, not code.

1. Implement `MetadataSourceClient` from `diglibrary.metadata.service`: one
   method, `search_releases`, returning canonical `ReleaseMetadata`.
2. Map the service payload into the canonical model. Normalize through
   `diglibrary.metadata.normalization` so your values compare correctly against
   other sources — in particular, Unicode NFC.
3. Give it a `MetadataSourceId` and register it in the ordered sequence passed
   to `MetadataService`. Its position in that sequence is its precedence.

Return an empty result when the source does not know the release. Never invent a
value to fill a field.

## Rules that apply to every extension

- Official APIs and protocols only. No reverse engineering, no scraping around
  a terms-of-service restriction, no GUI automation, no credential reuse from a
  browser session.
- Secrets come from environment variables, never from `config.toml`, and never
  appear in a log record or an exception message.
- Network access goes through an injected transport, so tests run offline and
  deterministically.
- Value objects are immutable: frozen dataclasses.
- Failures are translated into your own exception type at your boundary. Never
  let a library exception, such as a `urllib` error, escape to a caller.

## Why the shipped provider set is small

This project ships only providers whose use does not depend on circumventing
terms of service or copyright protection. That is a decision about what this
project distributes, not about what the architecture permits: the framework is
deliberately open.
