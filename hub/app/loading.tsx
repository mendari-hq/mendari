// SPDX-License-Identifier: Apache-2.0
export default function Loading() {
  return (
    <div className="loading-skeleton" aria-busy="true" aria-label="Loading">
      <div className="sk-bar sk-title" />
      <div className="sk-bar sk-lead" />
      <div className="sk-bar sk-card" />
      <div className="sk-bar sk-card" />
    </div>
  );
}
