// Test-only SCM process. It launches one isolated Router and owns that child.
using System;
using System.Diagnostics;
using System.IO;
using System.Security.AccessControl;
using System.Security.Principal;
using System.ServiceProcess;
using System.Text;

internal sealed class FixtureService : ServiceBase
{
    private readonly string directory;
    private readonly string python;
    private Process child;

    private FixtureService(string name, string directory, string python)
    {
        ServiceName = name;
        CanStop = true;
        AutoLog = false;
        this.directory = directory;
        this.python = python;
    }

    protected override void OnStart(string[] args)
    {
        // Keep the runtime's fixture identity accessible to the SCM account
        // and the disposable runner's elevated cleanup process.
        string identities = Path.Combine(directory, "router-identities.json");
        File.Copy(Path.Combine(directory, "identity-seed.json"), identities, true);
        var permissions = new FileSecurity();
        permissions.SetAccessRuleProtection(true, false);
        permissions.SetOwner(new SecurityIdentifier(WellKnownSidType.LocalSystemSid, null));
        foreach (var principal in new[] { WellKnownSidType.LocalSystemSid, WellKnownSidType.BuiltinAdministratorsSid })
            permissions.AddAccessRule(new FileSystemAccessRule(
                new SecurityIdentifier(principal, null), FileSystemRights.FullControl, AccessControlType.Allow));
        File.SetAccessControl(identities, permissions);

        var start = new ProcessStartInfo {
            FileName = python,
            Arguments = File.ReadAllText(Path.Combine(directory, "args.txt"), Encoding.UTF8),
            WorkingDirectory = File.ReadAllText(Path.Combine(directory, "workdir.txt"), Encoding.UTF8),
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true
        };
        start.EnvironmentVariables["PYTHONDONTWRITEBYTECODE"] = "1";
        start.EnvironmentVariables["PYTHONUTF8"] = "1";
        child = new Process { StartInfo = start };
        child.OutputDataReceived += (sender, item) => Append("router.stdout", item.Data);
        child.ErrorDataReceived += (sender, item) => Append("router.stderr", item.Data);
        try
        {
            child.Start();
            File.WriteAllText(Path.Combine(directory, "child.pid"), child.Id.ToString());
            child.BeginOutputReadLine();
            child.BeginErrorReadLine();
        }
        catch
        {
            StopChild();
            throw;
        }
    }

    private void Append(string name, string line)
    {
        if (line != null)
            File.AppendAllText(Path.Combine(directory, name), line + Environment.NewLine);
    }

    private void StopChild()
    {
        if (child == null)
            return;
        try
        {
            if (!child.HasExited)
            {
                child.Kill();
                if (!child.WaitForExit(6000))
                    throw new System.TimeoutException("Isolated router process did not stop");
            }
        }
        catch (InvalidOperationException)
        {
            // Process.Start failed before a process handle was acquired.
        }
        finally
        {
            child.Dispose();
            child = null;
        }
    }

    protected override void OnStop()
    {
        StopChild();
    }

    public static void Main(string[] args)
    {
        if (args.Length != 3)
            throw new ArgumentException("Expected the test service name, directory and Python executable");
        using (var service = new FixtureService(args[0], args[1], args[2]))
            Run(service);
    }
}
