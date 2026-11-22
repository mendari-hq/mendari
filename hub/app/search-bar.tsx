// SPDX-License-Identifier: Apache-2.0
"use client";

import Link from "next/link";
import type { ChangeEvent } from "react";

type SearchBarProps = {
  filterKey: string;
  q?: string;
  risk?: string;
  standard?: string;
  publisherType?: string;
  filtered: boolean;
};

export function SearchBar({ filterKey, q, risk, standard, publisherType, filtered }: SearchBarProps) {
  function submitOnSelect(event: ChangeEvent<HTMLSelectElement>) {
    event.currentTarget.form?.requestSubmit();
  }

  return (
    <form key={filterKey} className="search" role="search" action="/">
      <label>
        Search agents
        <input
          name="q"
          type="search"
          defaultValue={q ?? ""}
          placeholder="referral, prior authorization, triage…"
        />
      </label>
      <label>
        Risk level
        <select name="risk" defaultValue={risk ?? ""} onChange={submitOnSelect}>
          <option value="">Any</option>
          {[0, 1, 2, 3, 4].map((level) => (
            <option key={level} value={level}>{level}</option>
          ))}
        </select>
      </label>
      <label>
        Standard
        <select name="standard" defaultValue={standard ?? ""} onChange={submitOnSelect}>
          <option value="">Any</option>
          <option value="FHIR">FHIR R4</option>
          <option value="CDS Hooks">CDS Hooks</option>
        </select>
      </label>
      <label>
        Publisher
        <select name="publisher_type" defaultValue={publisherType ?? ""} onChange={submitOnSelect}>
          <option value="">Any</option>
          <option value="verified">Verified</option>
          <option value="institutional">Institutional</option>
          <option value="community">Community</option>
        </select>
      </label>
      <div className="search-actions">
        <button type="submit">Search</button>
        {filtered ? <Link href="/" className="clear-filters">Clear</Link> : null}
      </div>
    </form>
  );
}
