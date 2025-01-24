#!/usr/bin/ksh

# svcadm ssh restart from the ssh_util module? (backwards)
# hosts - /etc/inet/hosts
# pkg update - shutdown_cmd
# distro.restart_service() (cc_set_passwords)

function clean {
	ipadm delete-if bob3
	dladm rename-link bob3 vioif1
	ipadm delete-if vioif1
	ipadm delete-addr vioif0/ephdhcp
	ipadm show-addr -poADDROBJ vioif0/ | egrep '/(ci|eph|dhcp)' \
	    | xargs -l ipadm delete-addr
	route -f
	echo 172.27.10.254 > /etc/defaultrouter
	route -p add default 172.27.10.254
	echo nameserver 80.80.80.80 > /etc/resolv.conf
	cp /etc/inet/hosts{.sav,}
	userdel omnios
	zfs destroy rpool/home/omnios
	rm -rf /home/omnios
	rm -f /etc/sudoers.d/90-cloud-init-users
	rm -f /etc/user_attr.d/cloud-init-users
	cloud-init clean -ls
	touch /var/log/cloud-init.log
	pkg uninstall cpuid pciutils
	beadm destroy -Ffs omnios-r151039-1
	echo
	cloud-init status
}

function run {
	clean
	cloud-init init -l
	cloud-init init
	cloud-init modules --mode config
	cloud-init modules --mode final
}

[ "$1" = clean ] && rm -rf $PWD/root
python3 setup.py install --root=$PWD/root --init-system=smf

[ -n "$1" ] && "$@"

