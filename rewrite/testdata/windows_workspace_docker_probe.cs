using System;
using System.IO;

internal static class WorkspaceDockerProbe
{
    public static int Main(string[] args)
    {
        if (args.Length != 7 || args[0] != "--config" || args[2] != "--host" ||
            args[3] != "npipe:////./pipe/docker_engine" ||
            args[4] != "info" || args[5] != "--format" || args[6] != "{{.OSType}} {{.ID}}" ||
            !Path.IsPathRooted(args[1]) ||
            !String.Equals(Path.GetDirectoryName(args[1]), Directory.GetCurrentDirectory(),
                           StringComparison.OrdinalIgnoreCase))
        {
            return 81;
        }
        foreach (string key in new[] {
            "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_TLS_VERIFY",
            "DOCKER_CERT_PATH", "DOCKER_API_VERSION", "HTTP_PROXY", "HTTPS_PROXY", "USERPROFILE"
        })
        {
            if (Environment.GetEnvironmentVariable(key) != null) return 82;
        }
        string response = File.ReadAllText(Path.Combine(args[1], "daemon-reply.txt")).Trim();
        if (response == "unavailable") return 83;
        Console.WriteLine(response);
        return 0;
    }
}
