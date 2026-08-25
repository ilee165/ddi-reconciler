# Changelog

## [0.2.0](https://github.com/ilee165/ddi-reconciler/compare/v0.1.0...v0.2.0) (2026-08-25)


### Features

* federated truth — ServiceNow CMDB, snapshot, and scoped spatium sources ([ab22450](https://github.com/ilee165/ddi-reconciler/commit/ab2245000cfc95e9c3840ec5e0f9c58d85fa21d2))
* federated truth sources + review hardening fixes ([634986e](https://github.com/ilee165/ddi-reconciler/commit/634986efc770b8d91752293e09636f3c6ea42e7d))
* import reconciler core, providers, and offline test suite ([5263a35](https://github.com/ilee165/ddi-reconciler/commit/5263a35deebc89b866709ab06a1e5b19b06651a3))
* publish EdgeProvider/TruthSource Protocols as the extension contract ([85adef2](https://github.com/ilee165/ddi-reconciler/commit/85adef280686074689cbefdca251429acd379ecc))
* shipping example config with parse guarantee, snapshot format docs ([18847a5](https://github.com/ilee165/ddi-reconciler/commit/18847a5eb077554371bfb95770978cc1a6864c87))


### Bug Fixes

* **azure:** TTL preflight across the whole diff before any write ([d365a32](https://github.com/ilee165/ddi-reconciler/commit/d365a3269bc47d7407fafa5fc079a13d1422197c))
* **cli:** settle the per-edge account before reporting it ([fd687b3](https://github.com/ilee165/ddi-reconciler/commit/fd687b39d603f1ec3d666ede5232c4a537b8107e))
* **cloudflare:** act on every same-canonical record, surface skips, escape ids ([60baa16](https://github.com/ilee165/ddi-reconciler/commit/60baa167def4b58ef64930654666d71f227a06bb))
* **config:** FQDN-shaped managed-key names manage nothing too ([530e36a](https://github.com/ilee165/ddi-reconciler/commit/530e36ad2492e52ea7f826727906e5a2329cb1e6))
* **config:** reject managed-key names and zones no record can ever match ([c6c511b](https://github.com/ilee165/ddi-reconciler/commit/c6c511bcf908eeb85ac42280601162a47facb692))
* **model:** idempotent canonical_name, wildcard placement, TTL ceiling, zone validity ([7d430f3](https://github.com/ilee165/ddi-reconciler/commit/7d430f3b1836f33c57b06050e08206c82b618b77))
* **providers:** unsupported-type edge records occupy their owner name ([1ac9758](https://github.com/ilee165/ddi-reconciler/commit/1ac9758dd026811ac03260568759f89af5faa34b))
* **spatium:** echo the deployment's pagination keys; fail-closed read_verified ([2b6c039](https://github.com/ilee165/ddi-reconciler/commit/2b6c0394b53f32993bdaa20119c20b52c9415eb9))
