#include "bsp_siic.h"
#include "delay.h"
#include "icm20948.h" 
#include "icm20948_registers.h" 

#define ICM20948_DEV  ICM_20948_I2C_ADDR_AD0

extern IICInterface_t User_sIICDev;

/* 软件iic */

#define userconfig_USE_DELAY  1 //是否使用延迟功能,部分设备不支持高速,需要加入延迟才可正常通信
#define userconfig_DELAY_TIME 25 //延迟的时间,while( userconfig_DELAY_TIME-- ) \
                                      主频越高的单片机,数值需要越大.可以根据实际通信效果来调整.注：数值调整的过低,可能会导致时序不对而无法正常通信
									 									  
#define sIIC_SCL_GPIO_Port GPIOB
#define sIIC_SCL_Pin GPIO_Pin_10

#define sIIC_SDA_GPIO_Port GPIOB
#define sIIC_SDA_Pin GPIO_Pin_11

#define sIIC_SDA_H GPIO_WriteBit(sIIC_SDA_GPIO_Port,sIIC_SDA_Pin,Bit_SET)
#define sIIC_SDA_L GPIO_WriteBit(sIIC_SDA_GPIO_Port,sIIC_SDA_Pin,Bit_RESET)
#define sIIC_SCL_H GPIO_WriteBit(sIIC_SCL_GPIO_Port,sIIC_SCL_Pin,Bit_SET)
#define sIIC_SCL_L GPIO_WriteBit(sIIC_SCL_GPIO_Port,sIIC_SCL_Pin,Bit_RESET)

static uint8_t init_flag = 0;
static void siic_init(void)
{
	if( init_flag ) return;
	GPIO_InitTypeDef  GPIO_InitStructure;
	RCC_AHB1PeriphClockCmd(RCC_AHB1Periph_GPIOB, ENABLE);
	
	GPIO_InitStructure.GPIO_Pin =  sIIC_SCL_Pin|sIIC_SDA_Pin;
	GPIO_InitStructure.GPIO_Mode = GPIO_Mode_OUT;
	GPIO_InitStructure.GPIO_OType = GPIO_OType_OD;
	GPIO_InitStructure.GPIO_Speed = GPIO_Speed_100MHz;
	GPIO_InitStructure.GPIO_PuPd = GPIO_PuPd_UP;
	GPIO_Init(sIIC_SDA_GPIO_Port, &GPIO_InitStructure);
	sIIC_SDA_H;
	sIIC_SCL_H;
	init_flag = 1;
}

static void SDA_IN(void)
{
	GPIO_InitTypeDef GPIO_InitStructure = {0};
	GPIO_InitStructure.GPIO_Pin =  sIIC_SDA_Pin;
	GPIO_InitStructure.GPIO_Mode = GPIO_Mode_IN;
	GPIO_InitStructure.GPIO_Speed = GPIO_Speed_100MHz;
	GPIO_InitStructure.GPIO_PuPd = GPIO_PuPd_UP;
	GPIO_Init(sIIC_SDA_GPIO_Port,&GPIO_InitStructure);
}

static void SDA_OUT(void)
{
	GPIO_InitTypeDef  GPIO_InitStructure;
	GPIO_InitStructure.GPIO_Pin =  sIIC_SDA_Pin;
	GPIO_InitStructure.GPIO_Mode = GPIO_Mode_OUT;
	GPIO_InitStructure.GPIO_OType = GPIO_OType_OD;
	GPIO_InitStructure.GPIO_Speed = GPIO_Speed_100MHz;
	GPIO_InitStructure.GPIO_PuPd = GPIO_PuPd_UP;
	GPIO_Init(sIIC_SDA_GPIO_Port, &GPIO_InitStructure);
}

static uint8_t READ_SDA(void)
{
	return GPIO_ReadInputDataBit(sIIC_SDA_GPIO_Port,sIIC_SDA_Pin);
}

static void sIIC_Start(void)
{
	SDA_OUT();
	sIIC_SDA_H;
	sIIC_SCL_H;
	
	#if userconfig_USE_DELAY == 1
		volatile uint8_t delay = userconfig_DELAY_TIME;
		while(delay--);
	#endif
	
	sIIC_SDA_L;
	
	#if userconfig_USE_DELAY == 1
		delay = userconfig_DELAY_TIME;
		while(delay--);
	#endif
	
	sIIC_SCL_L;
}

static void sIIC_Stop(void)
{
    SDA_OUT();   // 设置 SDA 为输出
    sIIC_SCL_L;  // 拉低 SCL
    sIIC_SDA_L;  // 拉低 SDA
	
	#if userconfig_USE_DELAY == 1
		volatile uint8_t delay = userconfig_DELAY_TIME;
		while(delay--);
	#endif
	
    sIIC_SCL_H;  // 将 SCL 置为高电平
	
	#if userconfig_USE_DELAY == 1
		delay = userconfig_DELAY_TIME;
		while(delay--);
	#endif
	
    sIIC_SDA_H;  // 在 SCL 为高时，将 SDA 拉高生成停止信号
}

static uint8_t sIIC_WaitAck(uint32_t timeout)
{
    uint32_t time = 0;

    SDA_IN();    // 设置 SDA 为输入
    sIIC_SDA_H;  // 拉高 SDA
	
	#if userconfig_USE_DELAY == 1
		volatile uint8_t delay = userconfig_DELAY_TIME;
		while(delay--);
	#endif
	
    sIIC_SCL_H;  // 拉高 SCL，等待应答信号
	
	#if userconfig_USE_DELAY == 1
		delay = userconfig_DELAY_TIME;
		while(delay--);
	#endif
	
    while (READ_SDA()) {  // 检测从设备是否拉低 SDA，产生ACK信号
        time++;
        if (time > timeout) {
            sIIC_Stop();  // 超时则发送停止信号
            return 0;     // 无应答
        }
    }

    sIIC_SCL_L;  // 拉低 SCL，继续传输
    return 1;    // 收到应答
}

static void sIIC_Ack(void)
{
    sIIC_SCL_L;  // 拉低 SCL
    SDA_OUT();   // 设置 SDA 为输出
    sIIC_SDA_L;  // 拉低 SDA，发送 ACK
	
	#if userconfig_USE_DELAY == 1
		volatile uint8_t delay = userconfig_DELAY_TIME;
		while(delay--);
	#endif
	
    sIIC_SCL_H;  // 拉高 SCL，确认传输
	
	#if userconfig_USE_DELAY == 1
		delay = userconfig_DELAY_TIME;
		while(delay--);
	#endif
	
    sIIC_SCL_L;  // 拉低 SCL，结束 ACK 信号
}

static void sIIC_NAck(void)
{
    sIIC_SCL_L;  // 拉低 SCL
    SDA_OUT();   // 设置 SDA 为输出
    sIIC_SDA_H;  // 拉高 SDA，发送 NACK
	
	#if userconfig_USE_DELAY == 1
		volatile uint8_t delay = userconfig_DELAY_TIME;
		while(delay--);
	#endif
	
    sIIC_SCL_H;  // 拉高 SCL，确认传输
	
	#if userconfig_USE_DELAY == 1
		delay = userconfig_DELAY_TIME;
		while(delay--);
	#endif
	
    sIIC_SCL_L;  // 拉低 SCL，结束 NACK 信号
}

static void sIIC_SendByte(uint8_t byte)
{
    uint8_t i;

    SDA_OUT();  // 设置 SDA 为输出
    sIIC_SCL_L; // 拉低 SCL

    for (i = 0; i < 8; i++) {
        if (byte & 0x80) {
            sIIC_SDA_H;  // 发送高位
        } else {
            sIIC_SDA_L;  // 发送低位
        }
        byte <<= 1;       // 移动到下一位
		
		#if userconfig_USE_DELAY == 1
			volatile uint8_t delay = userconfig_DELAY_TIME;
			while(delay--);
		#endif
		
        sIIC_SCL_H;       // 拉高 SCL，传输当前位
		
		#if userconfig_USE_DELAY == 1
			delay = userconfig_DELAY_TIME;
			while(delay--);
		#endif
		
        sIIC_SCL_L;       // 拉低 SCL，准备下一位

    }
}

static uint8_t sIIC_ReadByte(uint8_t ack)
{
    uint8_t i, byte = 0;

    SDA_IN();  // 设置 SDA 为输入

    for (i = 0; i < 8; i++) {
        sIIC_SCL_L;  // 拉低 SCL

		#if userconfig_USE_DELAY == 1
			volatile uint8_t delay = userconfig_DELAY_TIME;
			while(delay--);
		#endif
		
        sIIC_SCL_H;  // 拉高 SCL，读取当前位
		
		#if userconfig_USE_DELAY == 1
			delay = userconfig_DELAY_TIME;
			while(delay--);
		#endif
		
        byte <<= 1;
        if (READ_SDA()) {
            byte |= 0x01;  // 读取到高电平
        }
    }

    if (!ack) {
        sIIC_NAck();  // 如果不需要应答，发送 NACK
    } else {
        sIIC_Ack();   // 需要应答则发送 ACK
    }

    return byte;
}

static IIC_Status_t IIC_Master_Transmit(uint16_t dev_addr, uint8_t *data, uint16_t size,uint32_t timeout)
{
    sIIC_Start();    // 发送起始信号

    sIIC_SendByte(dev_addr);        // 发送从设备地址（写模式）
    if (!sIIC_WaitAck(timeout)) {          // 等待应答
        sIIC_Stop();                // 如果没有应答，则停止通信
        return IIC_TIMEOUT;
    }

    for (uint16_t i = 0; i < size; i++) {
        sIIC_SendByte(data[i]);     // 逐字节发送数据
        if (!sIIC_WaitAck(timeout)) {      // 每发送一个字节后等待应答
            sIIC_Stop();            // 如果没有应答，则停止通信
            return IIC_TIMEOUT;
        }
    }

    sIIC_Stop();                    // 发送停止信号
    return IIC_OK;                       // 成功
}

static IIC_Status_t IIC_Master_Receive(uint16_t dev_addr, uint8_t *data, uint16_t size,uint32_t timeout)
{
    sIIC_Start();    // 发送起始信号

    sIIC_SendByte(dev_addr | 0x01); // 发送从设备地址（读模式）
    if (!sIIC_WaitAck(timeout)) {          // 等待应答
        sIIC_Stop();                // 如果没有应答，则停止通信
        return IIC_TIMEOUT;
    }

    for (uint16_t i = 0; i < size; i++) {
        data[i] = sIIC_ReadByte(i == (size - 1) ? 0 : 1); // 逐字节读取数据，最后一个字节发送NACK
    }

    sIIC_Stop();                    // 发送停止信号
    return IIC_OK;                       // 成功
}


static IIC_Status_t IIC_Mem_Write(uint16_t dev_addr, uint16_t mem_addr, uint8_t *data, uint16_t size,uint32_t timeout)
{
    sIIC_Start();                  // 发送起始信号
    
    sIIC_SendByte(dev_addr);       // 发送从设备地址（写模式）
    if (!sIIC_WaitAck(timeout)) return IIC_TIMEOUT; // 等待应答

    sIIC_SendByte(mem_addr);       // 发送内存/寄存器地址
    if (!sIIC_WaitAck(timeout)) return IIC_TIMEOUT; // 等待应答

    for (uint16_t i = 0; i < size; i++) {
        sIIC_SendByte(data[i]);    // 逐字节发送数据
        if (!sIIC_WaitAck(timeout)) return IIC_TIMEOUT; // 等待应答
    }
    
    sIIC_Stop();                   // 发送停止信号
    return IIC_OK;                      // 写入成功
}

static IIC_Status_t IIC_Mem_Read(uint16_t dev_addr, uint16_t mem_addr, uint8_t *data, uint16_t size,uint32_t timeout)
{
    sIIC_Start();                   // 发送起始信号
    
    sIIC_SendByte(dev_addr);        // 发送从设备地址（写模式）
    if (!sIIC_WaitAck(timeout)) return IIC_TIMEOUT;  // 等待应答

    sIIC_SendByte(mem_addr);        // 发送内存/寄存器地址
    if (!sIIC_WaitAck(timeout)) return IIC_TIMEOUT;  // 等待应答
    
    sIIC_Start();                    // 重新启动信号，准备读数据
    sIIC_SendByte(dev_addr | 0x01); // 发送从设备地址（读模式）
    if (!sIIC_WaitAck(timeout)) return IIC_TIMEOUT;  // 等待应答

    for (uint16_t i = 0; i < size; i++) {
        data[i] = sIIC_ReadByte(i == (size - 1) ? 0 : 1); // 逐字节读取数据，最后一个字节发送NACK
    }

    sIIC_Stop();                    // 发送停止信号
    return IIC_OK;                       // 读取成功
}

#pragma diag_suppress 223
static void iic_delayms(uint16_t ms)
{
	delay_ms(ms);
}
#pragma diag_default 223

icm20948_status_e icm20948_internal_write_i2c(uint8_t reg, uint8_t *data, uint32_t len, void *user)
{
    // 你的 write_reg 需要左移后的地址（0xD0），而学长传入的是 0x68，所以左移 1 位
    IIC_Status_t status = User_sIICDev.write_reg(ICM20948_DEV << 1, reg, data, len, 100);
    return (status == IIC_OK) ? ICM_20948_STAT_OK : ICM_20948_STAT_ERR;
}

icm20948_status_e icm20948_internal_read_i2c(uint8_t reg, uint8_t *buff, uint32_t len, void *user)
{
    IIC_Status_t status = User_sIICDev.read_reg(ICM20948_DEV << 1, reg, buff, len, 100);
    return (status == IIC_OK) ? ICM_20948_STAT_OK : ICM_20948_STAT_ERR;
}


void icm20948_init_i2c(icm20948_device_t *icm_device)
{
    // 1. 初始化设备结构体（清零并设置默认值）
    icm20948_init_struct(icm_device);

    // 2. 构建 serif 结构体，指向我们上面实现的读写函数
    static const icm20948_serif_t default_serif = {
        .write = icm20948_internal_write_i2c,
        .read  = icm20948_internal_read_i2c,
        .user  = NULL,   // 不需要额外用户数据
    };

    // 3. 链接 serif
    icm20948_link_serif(icm_device, &default_serif);

#ifdef CONFIG_ICM_20948_USE_DMP
    icm_device->_dmp_firmware_available = true;
#else
    icm_device->_dmp_firmware_available = false;
#endif
    icm_device->_firmware_loaded = false;
    icm_device->_last_bank = 255;
    icm_device->_last_mems_bank = 255;

}



//挂载驱动
IICInterface_t User_sIICDev = {
	.init = siic_init,
	.write = IIC_Master_Transmit , 
	.read = IIC_Master_Receive ,
	.write_reg = IIC_Mem_Write ,
	.read_reg = IIC_Mem_Read ,
	.delay_ms = iic_delayms
};



