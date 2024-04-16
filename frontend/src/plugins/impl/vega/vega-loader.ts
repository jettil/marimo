/* Copyright 2024 Marimo. All rights reserved. */
// import { loader as createLoader, read, typeParsers } from "vega-loader";
// @ts-expect-error - no types
import * as vl from "vega-loader";
import { DataFormat } from "./types";

// Re-export the vega-loader functions to add TypeScript types

export function read(
  data: string,
  format:
    | DataFormat
    | {
        type: DataFormat["type"];
        parse: "auto";
      }
    | undefined,
): Promise<object[]> {
  return vl.read(data, format);
}

export function createLoader(): {
  load: (url: string) => Promise<string>;
} {
  return vl.loader();
}

export type VegaType =
  | "boolean"
  | "integer"
  | "number"
  | "date"
  | "string"
  | "unknown";

export const typeParsers: Record<VegaType, (value: string) => unknown> =
  vl.typeParsers;
