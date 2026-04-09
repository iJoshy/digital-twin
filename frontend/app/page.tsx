import Twin from '@/components/twin';

export default function Home() {
  return (
    <main className="min-h-screen bg-gradient-to-br from-slate-50 to-gray-100">
      <div className="container mx-auto px-4 py-8">
        <div className="max-w-4xl mx-auto">
          <br/><br/><br/>
          <div className="h-[500px]">
            <Twin />
          </div>

        </div>
      </div>
    </main>
  );
}