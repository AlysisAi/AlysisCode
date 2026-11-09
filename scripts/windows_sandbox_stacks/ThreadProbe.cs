// Compiled separately with the installed .NET Framework compiler. Fixed,
// bounded observations only; this is never a replacement shell or release test.
using System;
using System.Threading;

static class ThreadProbe
{
    static void Report(string name, Func<bool> probe)
    {
        try { Console.WriteLine("ALYSIS_CLR_" + name + "=" + probe()); }
        catch (Exception error) {
            Console.WriteLine("ALYSIS_CLR_" + name + "=" + error.GetType().Name + ":" + error.HResult);
        }
    }
    static void Main()
    {
        Console.WriteLine("ALYSIS_CLR_ENTRY=True");
        Report("EVENT", delegate {
            using (var signal = new ManualResetEvent(false)) {
                signal.Set(); return signal.WaitOne(1000);
            }
        });
        Report("SEMAPHORE", delegate {
            using (var signal = new Semaphore(0, 1)) {
                signal.Release(); return signal.WaitOne(1000);
            }
        });
        Report("THREAD", delegate {
            var thread = new Thread(delegate() {});
            thread.IsBackground = true;
            thread.Start(); return thread.Join(2000);
        });
        int worker, completion;
        ThreadPool.GetMinThreads(out worker, out completion);
        Console.WriteLine("ALYSIS_CLR_MIN_WORKER=" + worker);
        ThreadPool.GetMaxThreads(out worker, out completion);
        Console.WriteLine("ALYSIS_CLR_MAX_WORKER=" + worker);
        // Keep this event alive until process exit even if the worker is late.
        var done = new ManualResetEvent(false);
        Report("POOL_QUEUE", delegate {
            return ThreadPool.QueueUserWorkItem(delegate { done.Set(); });
        });
        Console.WriteLine("ALYSIS_CLR_POOL_COMPLETED=" + done.WaitOne(3000));
        GC.KeepAlive(done);
    }
}
