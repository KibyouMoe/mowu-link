using System;
using System.IO;

internal static class WorkspaceDockerProbe
{
    public static int Main(string[] args)
    {
        if (args.Length < 5 || args[0] != "--config" || args[2] != "--host" ||
            args[3] != "npipe:////./pipe/docker_engine" ||
            !Path.IsPathRooted(args[1]) ||
            !String.Equals(Path.GetDirectoryName(args[1]), Directory.GetCurrentDirectory(),
                           StringComparison.OrdinalIgnoreCase))
        {
            return 81;
        }
        foreach (string key in new[] {
            "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_TLS_VERIFY",
            "DOCKER_CERT_PATH", "DOCKER_API_VERSION", "HTTP_PROXY", "HTTPS_PROXY"
        })
        {
            if (Environment.GetEnvironmentVariable(key) == null) continue;
            Console.Error.WriteLine("Unexpected environment variable: " + key);
            return 82;
        }
        foreach (string key in new[] {"USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP"})
        {
            if (String.Equals(Environment.GetEnvironmentVariable(key), args[1], StringComparison.OrdinalIgnoreCase)) continue;
            Console.Error.WriteLine("Profile directory not isolated for: " + key);
            return 84;
        }
        string home = Environment.GetEnvironmentVariable("HOMEDRIVE") + Environment.GetEnvironmentVariable("HOMEPATH");
        if (!String.Equals(home, args[1], StringComparison.OrdinalIgnoreCase)) return 84;
        if (args.Length == 7 && args[4] == "info" && args[5] == "--format" && args[6] == "{{.OSType}} {{.ID}}")
        {
            string reply = Path.Combine(args[1], "daemon-reply.txt");
            if (!File.Exists(reply)) reply = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "daemon-reply.txt");
            string response = File.ReadAllText(reply).Trim();
            if (response == "unavailable") return 83;
            Console.WriteLine(response);
            return 0;
        }
        string directory = AppDomain.CurrentDomain.BaseDirectory;
        string template = Path.Combine(directory, "fixture-container-template.json");
        bool simulateCreation = File.Exists(template);
        string image = "sha256:" + new string('b', 64);
        if (simulateCreation && args.Length == 7 && args[4] == "image" && args[5] == "inspect" && args[6] == image)
        {
            Console.WriteLine("[{\"Id\":\"" + image + "\",\"Os\":\"linux\",\"Config\":{}}]");
            return 0;
        }
        if (args.Length < 6 || args[4] != "container") return 81;
        string container = Path.Combine(directory, "fixture-container.json");
        string label = Path.Combine(directory, "fixture-container-label.txt");
        string id = new string('a', 64);
        if (simulateCreation && args[5] == "create" && !File.Exists(container))
        {
            string tag = Option(args, "--label");
            const string prefix = "io.mowulink.assignment=";
            Guid token;
            if (!tag.StartsWith(prefix, StringComparison.Ordinal) ||
                !Guid.TryParseExact(tag.Substring(prefix.Length), "D", out token) ||
                Option(args, "--name") != "mowulink-" + token.ToString("D") ||
                Option(args, "--pull") != "never" || Option(args, "--network") != "none" ||
                Option(args, "--user") != "65534:65534" || Option(args, "--cap-drop") != "ALL" ||
                Option(args, "--security-opt") != "no-new-privileges" || !Array.Exists(args, arg => arg == "--read-only") ||
                args[args.Length - 2] != image || args[args.Length - 1] != "infinity") return 81;
            string created = File.ReadAllText(template).Replace("fixture-token", token.ToString("D"))
                .Replace("\"Status\":\"running\"", "\"Status\":\"created\"")
                .Replace("\"Running\":true", "\"Running\":false");
            File.WriteAllText(label, "label=" + tag);
            File.WriteAllText(container, created);
            Console.WriteLine(id);
            return 0;
        }
        if (simulateCreation && args[5] == "start" && args.Length == 7 && args[6] == id && File.Exists(container))
        {
            string running = File.ReadAllText(container).Replace("\"Status\":\"created\"", "\"Status\":\"running\"")
                .Replace("\"Running\":false", "\"Running\":true");
            File.WriteAllText(container, running);
            Console.WriteLine(id);
            return 0;
        }
        if (args[5] == "ls" && args.Length == 12 &&
            args[6] == "--all" && args[7] == "--no-trunc" && args[8] == "--filter" &&
            args[10] == "--format" && args[11] == "{{.ID}}")
        {
            if (File.Exists(container) && args[9] == File.ReadAllText(label).Trim()) Console.WriteLine(id);
            return 0;
        }
        if (args[5] == "inspect" && args.Length == 7 && args[6] == id && File.Exists(container))
        {
            Console.WriteLine(File.ReadAllText(container));
            return 0;
        }
        if (simulateCreation && args[5] == "exec" && args.Length == 10 && args[6] == id &&
            args[7] == "/bin/sh" && args[8] == "-c" && args[9].StartsWith("mkdir -p results;") &&
            File.Exists(container)) return 0;
        if (args[5] == "exec" && args.Length == 13 && args[6] == id && args[7] == "python3" &&
            args[8] == "-I" && args[9] == "-c" &&
            (args[12] == "artifact.bin" || simulateCreation && args[12] == "results/sample.bin") &&
            File.Exists(container))
        {
            using (Stream output = Console.OpenStandardOutput())
            using (Stream source = File.OpenRead(Path.Combine(directory, "artifact.bin")))
                source.CopyTo(output);
            return 0;
        }
        if (args[5] == "rm" && args.Length == 8 && args[6] == "--force" && args[7] == id && File.Exists(container))
        {
            File.Delete(container);
            Console.WriteLine(id);
            return 0;
        }
        return 81;
    }

    private static string Option(string[] args, string name)
    {
        int index = Array.IndexOf(args, name, 6);
        return index >= 0 && index + 1 < args.Length ? args[index + 1] : "";
    }
}
