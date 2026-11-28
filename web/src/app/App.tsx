import { useEffect, useState } from "react";

import { GatewayClient } from "@/api/client";
import { Button } from "@/ui/Button";
import { cn } from "@/ui/cn";

import { copy } from "./copy";

type GatewayStatus = "checking" | "online" | "offline";

export interface AppProps {
  readonly client?: GatewayClient;
}

const defaultClient = new GatewayClient();

export function App({ client = defaultClient }: AppProps) {
  const [status, setStatus] = useState<GatewayStatus>("checking");
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    client.listSessions({ limit: 1 }).then(
      () => {
        if (!controller.signal.aborted) {
          setStatus("online");
        }
      },
      () => {
        if (!controller.signal.aborted) {
          setStatus("offline");
        }
      },
    );
    return () => {
      controller.abort();
    };
  }, [client, attempt]);

  const retry = () => {
    setStatus("checking");
    setAttempt((value) => value + 1);
  };

  return (
    <main className="flex h-full items-center justify-center p-6">
      <section className="w-full max-w-md rounded-xl border border-border bg-surface p-8 shadow-card">
        <h1 className="text-2xl font-semibold">{copy.appName}</h1>
        <p className="mt-1 text-muted-foreground">{copy.tagline}</p>
        <p className="mt-6 flex items-center gap-2 text-sm" role="status">
          <span
            aria-hidden="true"
            className={cn(
              "size-2 rounded-full",
              status === "online" && "bg-ok",
              status === "offline" && "bg-err",
              status === "checking" && "bg-faint",
            )}
          />
          {copy.gateway[status]}
        </p>
        {status === "offline" && (
          <div className="mt-3 space-y-3 text-sm text-muted-foreground">
            <p>{copy.gateway.offlineHint}</p>
            <Button variant="outline" size="sm" onClick={retry}>
              {copy.gateway.retry}
            </Button>
          </div>
        )}
        <p className="mt-6 text-sm text-faint">{copy.scaffold}</p>
      </section>
    </main>
  );
}
