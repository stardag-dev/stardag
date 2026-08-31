"""The registry-live tier: real Modal workers against a real registry.

Two tiers already cover half of this each, and neither covers the crossing:

- ``lib/stardag``'s ``modal_live`` tier runs real Modal containers, but
  every registry in it is a ``NoOpRegistry`` subclass, so claim arbitration
  and reactive wake-ups are simulated in-process.
- The API's own suite runs against real Postgres, but nothing in it drives
  a Modal worker.

The reactive loop is precisely that crossing: a worker writes status to the
registry, the registry flags wake candidates, and the *worker* spawns the
next scheduler tick when no scheduler is live. Exercising it needs a
registry a Modal container can reach over the network, which means a
deployed one.

So this tier deploys the branch's API to a throwaway Modal environment --
with its Postgres inside the API's own container, so there is no database
account, no provisioning and no teardown beyond deleting the environment --
and runs scenarios against it with workers on real Modal.
"""
