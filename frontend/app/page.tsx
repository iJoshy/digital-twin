import Twin from '@/components/twin';

export default function Home() {
  return (
    <main className="min-h-screen bg-slate-100">
      <div className="mx-auto flex min-h-screen max-w-5xl flex-col px-4 py-4 sm:py-6">
        <div className="min-h-[680px] flex-1">
          <Twin />
        </div>
      </div>
    </main>
  );
}
