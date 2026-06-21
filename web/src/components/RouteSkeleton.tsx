// T-0365: instant skeleton for the lazy-route <Suspense> boundary (and reusable
// as a page-load placeholder). Code-splitting the routes means a brief moment
// while the route chunk loads — paint a content-shaped skeleton then, not a
// blank "Loading…". The shapes loosely match a typical page: a title + a few
// rows / cards.
function Bar({ w, h = 14, mb = 10 }: { w: string; h?: number; mb?: number }) {
  return <div className="mc-skeleton" style={{ width: w, height: h, marginBottom: mb }} />;
}

export function RouteSkeleton() {
  return (
    <div className="container py-4" aria-busy="true" aria-label="Loading">
      <Bar w="40%" h={22} mb={20} />
      <div className="d-flex gap-3 mb-4" style={{ flexWrap: "wrap" }}>
        {[0, 1, 2, 3].map((i) => (
          <div key={i} className="mc-skeleton" style={{ width: "9rem", height: "4.5rem" }} />
        ))}
      </div>
      <Bar w="100%" h={40} />
      <Bar w="100%" h={40} />
      <Bar w="85%" h={40} />
      <Bar w="92%" h={40} />
    </div>
  );
}
