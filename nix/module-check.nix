let
  flake = builtins.getFlake (toString ./..);
  nixpkgs = flake.inputs.nixpkgs;
  system = nixpkgs.lib.nixosSystem {
    system = "x86_64-linux";
    modules = [
      flake.nixosModules.default
      ({ ... }: {
        boot.loader.grub.device = "nodev";
        fileSystems."/".device = "/dev/null";
        fileSystems."/".fsType = "ext4";
        services.radio-listener = {
          enable = true;
          meter.enable = true;
          logshipUrl = "https://example.com";
          accountId = "11111111-2222-4333-8444-555555555555";
          tokenFile = "/etc/radio-listener/token.env";
        };
        system.stateVersion = "25.11";
      })
    ];
  };
in builtins.toJSON {
  execStart = system.config.systemd.services.radio-listener.serviceConfig.ExecStart;
  path = map toString system.config.systemd.services.radio-listener.path;
}
