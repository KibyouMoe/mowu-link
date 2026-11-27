// Windows SCM entry for one explicitly configured Router from an installed release.
using System;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.ServiceProcess;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;

internal sealed class RouterService : ServiceBase
{
    private readonly ProcessStartInfo start;
    private readonly string readinessFile;
    private readonly object lifecycle = new object();
    private Process child;
    private volatile bool stopping;

    private static string FileArgument(string value)
    {
        if (!Path.IsPathRooted(value) || value.IndexOfAny(new[] {'"', '\r', '\n'}) >= 0
                || value.EndsWith("\\", StringComparison.Ordinal))
            throw new ArgumentException("Expected an absolute file path");
        return "\"" + value + "\"";
    }

    private RouterService(string[] args)
    {
        ServiceName = args[0];
        CanStop = true;
        CanShutdown = true;
        AutoLog = false;
        IPAddress address;
        int port;
        if (!IPAddress.TryParse(args[7], out address) || !Int32.TryParse(args[8], out port) || port < 0 || port > 65535)
            throw new ArgumentException("Expected an IP address and TCP port");
        readinessFile = Path.Combine(Path.GetDirectoryName(args[3]), ServiceName + ".ready.json");
        start = new ProcessStartInfo {
            FileName = args[1],
            WorkingDirectory = args[2],
            Arguments = "-B -E -S -X utf8 -m rewrite.service router --stop-with-stdin"
                + " --db " + FileArgument(args[3]) + " --identities " + FileArgument(args[4])
                + " --cert " + FileArgument(args[5]) + " --key " + FileArgument(args[6])
                + " --bind " + address + " --port " + port,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            StandardOutputEncoding = Encoding.UTF8
        };
    }

    protected override void OnStart(string[] args)
    {
        lock (lifecycle)
        {
            stopping = false;
            RequestAdditionalTime(15000);
            File.Delete(readinessFile);
            child = Process.Start(start);
            try
            {
                var line = child.StandardOutput.ReadLineAsync();
                if (!line.Wait(10000) || line.Result == null || child.HasExited)
                    throw new InvalidOperationException("Router did not publish readiness");
                var json = new JavaScriptSerializer();
                var ready = json.DeserializeObject(line.Result) as System.Collections.Generic.Dictionary<string, object>;
                if (ready == null || !Object.Equals(ready["component"], "router")
                        || !Object.Equals(ready["tls"], true) || !(ready["port"] is int)
                        || (int)ready["port"] < 1 || (int)ready["port"] > 65535)
                    throw new InvalidOperationException("Invalid Router readiness");
                ready.Add("pid", child.Id);
                string temporary = readinessFile + "." + Guid.NewGuid().ToString("N");
                try
                {
                    File.WriteAllText(temporary, json.Serialize(ready), new UTF8Encoding(false));
                    File.Move(temporary, readinessFile);
                }
                finally
                {
                    File.Delete(temporary);
                }
                child.Exited += ChildExited;
                child.EnableRaisingEvents = true;
            }
            catch
            {
                StopRouter();
                throw;
            }
        }
    }

    private void ChildExited(object sender, EventArgs args)
    {
        ThreadPool.QueueUserWorkItem(_ => {
            lock (lifecycle)
            {
                if (stopping || !Object.ReferenceEquals(sender, child))
                    return;
                ExitCode = 1067;
            }
            Stop();
        });
    }

    private void StopRouter()
    {
        stopping = true;
        if (child != null)
        {
            try
            {
                try { child.StandardInput.Close(); }
                catch (IOException) { }
                if (!child.WaitForExit(6000))
                {
                    child.Kill();
                    if (!child.WaitForExit(4000))
                        throw new System.TimeoutException("Router process did not exit");
                }
            }
            finally
            {
                child.Dispose();
                child = null;
            }
        }
        File.Delete(readinessFile);
    }

    protected override void OnStop()
    {
        lock (lifecycle)
        {
            RequestAdditionalTime(12000);
            StopRouter();
        }
    }

    protected override void OnShutdown()
    {
        lock (lifecycle)
            StopRouter();
    }

    public static void Main(string[] args)
    {
        if (args.Length != 9)
            throw new ArgumentException("Expected the registered Router service arguments");
        using (var service = new RouterService(args))
            Run(service);
    }
}
